"""
End-of-Day (EOD) digest email.

Sends each active user one daily summary of THEIR OWN activity today plus a snapshot
of their open workload. Intended to run once daily from a Railway Cron Job:

    python manage.py send_eod_digest --i-am-sending-to-real-people

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
  --user <email>     restrict to a single user (pre-flight); also skips the aggregate
                     send. Still a REAL send, so it needs the interlock below.
  --date <YYYY-MM-DD> override "today" (IST) for back-testing against real activity
  --out <PATH>       render the CEO aggregate HTML to PATH and exit. Sends nothing, logs
                     nothing, and issues NO write query of any kind (not even the
                     SystemSettings get_or_create) - safe to run from a local machine over
                     a READ-ONLY connection to the production database.
  --to <EMAIL>       repeatable. Overrides the aggregate recipients with these addresses:
                     the role='CEO' lookup and the Admin/HR address-merge are skipped
                     entirely, and every address gets the richer CEO body. Also skips the
                     individual digests - the mirror of what --user does to the aggregate.
  --i-am-sending-to-real-people
                     Safety interlock. REQUIRED for any real send: without it, and without
                     --dry-run / --out / --to, the command names the recipients it resolved,
                     refuses, and exits 1 before a single email leaves. This is what stops a
                     local run pointed at the production database from mailing the company.

Every run prints DATABASES['default'] HOST (never the password) before doing anything else,
so the operator can see at a glance which database they are pointed at.

The cron invocation must therefore carry the interlock:

    python manage.py send_eod_digest --i-am-sending-to-real-people
"""

import logging
import os
import sys
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
        parser.add_argument('--out', type=str, default='',
                            help='Render the CEO aggregate HTML to this path and exit. '
                                 'Sends nothing and writes nothing to the database.')
        parser.add_argument('--to', action='append', default=[], metavar='EMAIL',
                            help='Override the aggregate recipients (repeatable). Skips the '
                                 "role='CEO' lookup, the Admin/HR merge, and the individual digests.")
        parser.add_argument('--i-am-sending-to-real-people', action='store_true',
                            help='Safety interlock. Required for a real send; without it (and '
                                 'without --dry-run/--out/--to) the command refuses and exits 1.')

    def handle(self, *args, **options):
        # Before anything else: say which database this run is pointed at. HOST (and NAME)
        # only - the same dict also holds PASSWORD, which is never printed.
        db_conf = settings.DATABASES.get('default', {})
        db_host = db_conf.get('HOST') or '(none - local/socket)'
        self.stdout.write(f"[db] host={db_host} name={db_conf.get('NAME') or '(unset)'}")

        from projects.models import Task, ActivityLog, UserProfile, Project, Issue
        from projects.notifications import send_notification, _log

        dry_run   = options['dry_run']
        only_email = options['user'].strip().lower()
        out_path   = (options.get('out') or '').strip()
        to_emails  = [e.strip() for e in (options.get('to') or []) if e.strip()]
        armed      = options.get('i_am_sending_to_real_people', False)

        if options['date']:
            try:
                today = date_cls.fromisoformat(options['date'])
            except ValueError:
                raise CommandError(f"--date must be YYYY-MM-DD, got {options['date']!r}")
        else:
            today = timezone.localdate()  # IST calendar date (TIME_ZONE=Asia/Kolkata)

        app_url = getattr(settings, 'APP_BASE_URL',
                          'https://horizon-solar-pms-production.up.railway.app')
        date_str = today.strftime('%d %b %Y')

        # --- Safety interlock -----------------------------------------------------------
        # A real send requires --i-am-sending-to-real-people. --dry-run and --out send
        # nothing, and --to redirects the mail to the operator's own address(es), so those
        # three are exempt. The check lives HERE, before anything is sent: refusing inside
        # _run_aggregate would be too late, because the individual digests run first and
        # would already have gone out to the whole company.
        if not (dry_run or out_path or to_emails or armed):
            recipients_line = '\n                  '.join(self._aggregate_recipient_labels())
            self.stderr.write(
                'REFUSING TO SEND: --i-am-sending-to-real-people was not given.\n'
                f'  database host : {db_host}\n'
                '  would send    : individual digests to every active, non-excluded user\n'
                f'  aggregate to  : {recipients_line}\n'
                'Re-run with --dry-run, --out PATH or --to EMAIL to test safely, or with '
                '--i-am-sending-to-real-people for the real send.'
            )
            sys.exit(1)

        # --- --out: render the CEO aggregate to a file and stop. No send, no DB write. ---
        if out_path:
            self._run_aggregate(today, date_str, app_url, dry_run=False, out_path=out_path)
            return

        # --- --to: the aggregate IS the target, so the individual digests are skipped -
        # the mirror image of --user, which skips the aggregate. ---
        if to_emails:
            self.stdout.write('Skipping individual digests (--to targets the aggregate only).')
            self._run_aggregate(today, date_str, app_url, dry_run, to_emails=to_emails)
            return

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

        date_str = today.strftime('%d %b %Y')   # same value handle() computed above
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

    def _ceo_extra_metrics(self, today):
        """CEO-only additions to the aggregate: two payment-milestone figures (invoiced vs
        received today — kept SEPARATE, they are distinct DateFields), material deliveries
        received today, and the single most-active user today. Pure queries against existing
        dated fields (see CEO metrics audit) — no schema change. 'Orders placed' is
        deliberately absent: no PO/procurement model exists and a BOQ proxy would mislabel."""
        from projects.models import PaymentMilestone, DeliveryChallan, ActivityLog, UserProfile

        # A/B — invoiced today vs received (paid) today. Separate DateFields, not conflated.
        invoiced = PaymentMilestone.objects.filter(invoice_date=today).count()
        paid     = PaymentMilestone.objects.filter(received_date=today).count()

        # C — distinct Delivery Challans with at least one line item received (GRN) today.
        # Count DISTINCT challans, NOT line items (line items inflate the number).
        deliveries = (
            DeliveryChallan.objects
            .filter(line_items__grn_date=today)
            .distinct()
            .count()
        )

        # E — most active user today = most logged actions, excluding login/logout auth noise
        # (now carry action_code user_login/user_logout) and system rows (actor NULL). Uses
        # Count('pk') = every action, NOT distinct entity_id (that dedup is the individual
        # digest's goal, not this one).
        top = (
            ActivityLog.objects
            .filter(timestamp__date=today, actor__isnull=False)
            .exclude(action_code__in=['user_login', 'user_logout'])
            .values('actor')
            .annotate(c=Count('pk'))
            .order_by('-c')
            .first()
        )
        most_active = None
        if top:
            prof = (UserProfile.objects.select_related('user')
                    .filter(pk=top['actor']).first())
            if prof:
                most_active = {
                    'name':  prof.user.get_full_name() or prof.user.username,
                    'count': top['c'],
                }

        return {
            'invoiced_today':   invoiced,
            'paid_today':       paid,
            'deliveries_today': deliveries,
            'most_active':      most_active,   # None -> template shows "No activity recorded today"
        }

    def _aggregate_recipient_labels(self):
        """'Label <email>' for everyone a real aggregate run would reach. SELECT-only, and
        used solely by the interlock's refusal message - it never sends anything."""
        from projects.models import UserProfile

        labels = [
            f"Admin <{(getattr(settings, 'ADMIN_DIGEST_EMAIL', '') or '').strip() or '(unset)'}>",
            f"HR <{(getattr(settings, 'HR_DIGEST_EMAIL', '') or '').strip() or '(unset)'}>",
        ]
        for p in (UserProfile.objects
                  .filter(role='CEO', is_active=True, user__is_active=True)
                  .select_related('user')
                  .order_by('user__first_name', 'user__username')):
            email = (p.user.email or '').strip()
            if email:
                labels.append(f"CEO {p.user.get_full_name() or p.user.username} <{email}>")
        return labels

    def _run_aggregate(self, today, date_str, app_url, dry_run, out_path='', to_emails=None):
        from projects.models import UserProfile
        # Imported inside the method, matching this command's existing convention of
        # deferring every projects.* import until handle() runs.
        from projects.reports import build_user_status_rows

        admin_email = (getattr(settings, 'ADMIN_DIGEST_EMAIL', '') or '').strip()
        hr_email    = (getattr(settings, 'HR_DIGEST_EMAIL', '') or '').strip()

        totals = self._company_totals(today)
        self.stdout.write(
            f"[aggregate] company totals ({date_str}): assigned={totals['assigned']} "
            f"started={totals['started']} closed={totals['closed']} "
            f"raised={totals['issues_raised']} resolved={totals['issues_resolved']}"
        )

        # Base context = the SAME 5-metric email Admin/HR have always received. CEO reuses it
        # and only ADDS a flag + extra data; the extra sections in the template gate on
        # `show_ceo_sections`, which Admin/HR never set, so their bodies are byte-identical.
        base_ctx = {
            'recipient_name': 'Team',
            'heading':        'Company-wide EOD Summary',
            'intro':          "Here is the whole team's activity today (IST).",
            'assigned_label': 'assigned across the team',
            'footer_note':    'Company-wide totals across all users for today (IST).',
            'date_str':       date_str,
            'metrics':        totals,
            'app_url':        app_url,
        }
        base_html = render_to_string('projects/email/eod_digest.html', base_ctx)
        base_text = render_to_string('projects/email/eod_digest.txt', base_ctx)

        # CEO variant: base + the three extra sections.
        ceo = self._ceo_extra_metrics(today)
        ma = ceo['most_active']
        self.stdout.write(
            f"[aggregate][ceo] invoiced_today~={ceo['invoiced_today']} "
            f"paid_today={ceo['paid_today']} deliveries_today={ceo['deliveries_today']} "
            f"most_active={(ma['name'] + ' (' + str(ma['count']) + ')') if ma else '(none)'}"
        )
        # Per-user status table — the SAME builder the /reports/user-status/ page calls, so
        # the email and the page can never report different figures for the same day. Six
        # queries, flat in the number of users. Added to the CEO context ONLY: Admin/HR
        # never set show_ceo_sections, and base_ctx is untouched above, so their bodies stay
        # byte-identical to what they received before this change.
        user_status = build_user_status_rows(today)
        self.stdout.write(
            f"[aggregate][ceo] per-user rows={len(user_status['rows'])} "
            f"tasks={user_status['totals']['tasks_assigned']} "
            f"overdue={user_status['totals']['overdue']} "
            f"not_logged_in={user_status['totals']['not_logged_in_count']}"
        )
        ceo_ctx = dict(base_ctx)
        ceo_ctx['show_ceo_sections'] = True
        ceo_ctx['ceo'] = ceo
        ceo_ctx['user_status'] = user_status
        # Reuses app_url, which is already resolved by handle() through the same
        # getattr(settings, 'APP_BASE_URL', <hardcoded Railway URL>) fallback this command
        # has always used. Deliberately not adding the setting — see DEFERRED G2.
        ceo_ctx['user_status_url'] = f"{app_url.rstrip('/')}/reports/user-status/"
        ceo_html = render_to_string('projects/email/eod_digest.html', ceo_ctx)
        ceo_text = render_to_string('projects/email/eod_digest.txt', ceo_ctx)

        # --out: write the CEO body to a file and stop. Everything executed above is
        # SELECT-only, and the master-switch read below deliberately avoids
        # SystemSettings.get() (a get_or_create, i.e. a WRITE when the row is missing) in
        # favour of a plain .filter(pk=1).first(), so the whole path is safe on a READ-ONLY
        # connection to production. A missing row is treated as the switch being off.
        if out_path:
            from projects.models import SystemSettings
            row = SystemSettings.objects.filter(pk=1).first()
            missing = ' (no SystemSettings row - treated as OFF)' if row is None else ''
            state = 'ON' if (row is not None and row.email_enabled) else 'OFF'
            self.stdout.write(f'[out] email master switch: {state}{missing}')

            directory = os.path.dirname(os.path.abspath(out_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as fh:
                fh.write(ceo_html)
            self.stdout.write(
                f'[out] wrote CEO aggregate HTML ({len(ceo_html)} chars) to {out_path}')
            self.stdout.write('[out] nothing sent, nothing logged, no rows written.')
            return

        # --to: the given addresses ARE the recipient set. The role='CEO' lookup and the
        # Admin/HR address-merge below are skipped entirely; every address gets the CEO body.
        if to_emails:
            self.stdout.write('[aggregate] --to override, recipients: ' + ', '.join(to_emails))
            entries = [('--to', e, ceo_text, ceo_html, None) for e in to_emails]
            if dry_run:
                for label, email, *_ in entries:
                    self.stdout.write(f'[aggregate][dry-run] would send to {label}: {email} '
                                      f'(with CEO extra sections)')
                return
            self._deliver(entries, date_str)
            return

        # (label, email, text_body, html_body, profile). Admin/HR are FIXED addresses sharing
        # the base body. CEO recipients are resolved DYNAMICALLY from role='CEO' (CEO is its
        # own UserProfile.role and CEO accounts already exist) — supports multiple CEOs, each
        # gets the richer CEO body. CEO-role users are already excluded from the INDIVIDUAL
        # digest (EOD_DIGEST_EXCLUDED_ROLES), so this aggregate is their only EOD email.
        core = [
            ('Admin', admin_email, base_text, base_html, None),
            ('HR',    hr_email,    base_text, base_html, None),
        ]
        # Admin/HR keep their existing contract: a placeholder in EITHER aborts the aggregate.
        core_placeholder = [label for label, email, *_ in core if 'REPLACE_WITH' in email]

        ceo_profiles = (
            UserProfile.objects
            .filter(role='CEO', is_active=True, user__is_active=True)
            .select_related('user')
            .order_by('user__first_name', 'user__username')
        )
        ceo_recipients, seen_ceo = [], set()
        for p in ceo_profiles:
            email = (p.user.email or '').strip()
            key = email.lower()
            if not email or key in seen_ceo:
                continue   # skip CEOs with no email, and de-dup a shared address
            seen_ceo.add(key)
            label = f"CEO {p.user.get_full_name() or p.user.username}"
            ceo_recipients.append((label, email, ceo_text, ceo_html, p))

        if dry_run:
            for label, email, *_ in core:
                self.stdout.write(f"[aggregate][dry-run] would send to {label}: {email or '(unset)'}")
            if ceo_recipients:
                for label, email, *_ in ceo_recipients:
                    self.stdout.write(f"[aggregate][dry-run] would send to {label}: {email} (with CEO extra sections)")
            else:
                self.stdout.write(
                    "[aggregate][dry-run] no active CEO-role user with an email — CEO digest "
                    "skipped (Admin/HR unaffected)."
                )
            if core_placeholder:
                self.stdout.write(
                    f"[aggregate][dry-run] WARNING: {', '.join(core_placeholder)} still contain "
                    f"a placeholder — a real run would ABORT the aggregate send here."
                )
            return

        # Real run: refuse to send while an Admin/HR placeholder remains. Raised AFTER the
        # individual digests, so only the aggregate portion is aborted (per spec).
        if core_placeholder:
            raise CommandError(
                f"Aggregate digest NOT sent — {', '.join(core_placeholder)} still contain "
                f"'REPLACE_WITH'. Set the real address(es) in settings/env. Individual "
                f"digests were already processed and are unaffected."
            )

        if not ceo_recipients:
            self.stdout.write("[aggregate] no active CEO-role user with an email — CEO digest skipped (Admin/HR sent normally).")

        # Merge by email so a CEO who happens to share the Admin/HR address gets ONE email —
        # the richer CEO body (CEO entries are appended last, so they win the dict key).
        send_map = {}
        for label, email, text_body, html_body, prof in core + ceo_recipients:
            e = (email or '').strip()
            if e:
                send_map[e.lower()] = (label, e, text_body, html_body, prof)

        self._deliver(send_map.values(), date_str)

    def _deliver(self, entries, date_str):
        """Send the aggregate email to each (label, email, text_body, html_body, profile)
        entry. Unchanged from the loop that used to live inline in _run_aggregate; shared
        now so the --to override reuses exactly the same send + logging path."""
        from django.contrib.auth.models import User
        from projects.notifications import send_aggregate_email

        subject = f'Company-wide Horizon Solar EOD Summary — {date_str}'
        for label, val, text_body, html_body, prof in entries:
            # Resolve the address to a UserProfile (by email) so the send lands in
            # NotificationLog; if there's no matching account, it still sends but is
            # recorded only in the application log (see send_aggregate_email). CEO entries
            # already carry their profile, so no lookup is needed for those.
            if prof is None:
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
