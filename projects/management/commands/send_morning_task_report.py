"""
Daily morning task report, by email and WhatsApp.

Each person gets ONE report a morning: their delayed, due-today and no-due-date counts,
the delayed and due-today tasks, and a link. Who gets which scope ("all active
projects", "your projects", "your tasks") and every figure come from
projects.task_report.build_task_reports(); this command only renders and sends.

    python manage.py send_morning_task_report --i-am-sending-to-real-people

SENDING goes through notifications.send_notification(), so the master switches
(SystemSettings.email_enabled / whatsapp_enabled), each user's email / WhatsApp
preference and NotificationLog all apply. Both channels log template_name
'daily_task_report'. WhatsApp uses the Interakt template 'daily_task_report' with seven
parameters: one header (the date) and six body values (first name, scope, delayed, due
today, no due date, link).

Options:
  --dry-run          print each recipient's role, scope and three counts, and what would
                     be sent, then the totals. Sends nothing and writes nothing.
  --no-whatsapp      email only (until Interakt approves the template).
  --only-user USERNAME
                     restrict to one recipient. Still a real send without --dry-run.
  --to EMAIL         repeatable; needs --only-user. Sends that person's EMAIL to these
                     addresses instead of to them, through send_aggregate_email with no
                     log recipient, so it never counts as their send for the day. No
                     WhatsApp. Exempt from the interlock, as in send_eod_digest: it goes
                     only to addresses typed on the command line.
  --i-am-sending-to-real-people
                     safety interlock. Without it (and without --dry-run / --to) the
                     command names the recipients it resolved and exits 1.

IDEMPOTENT PER CHANNEL. A recipient who already has a 'sent' NotificationLog row for
'daily_task_report' on today's date (IST) on a channel is not sent that channel again.
A run that failed on WhatsApp can be repeated without repeating the email.

CIRCUIT BREAKER. More than MAX_RECIPIENTS recipients aborts before anything is sent:
that is several times the company's head count, so it means a scope or recipient bug.

Every run prints DATABASES['default'] HOST and NAME first (never the password).
"""
import sys
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone

from projects.models import NotificationLog
from projects.notifications import send_aggregate_email, send_notification
from projects.task_report import build_task_reports

#: The Interakt template name, and the NotificationLog.template_name both channels log
#: under. The duplicate check reads it, so it must not change between two runs a day.
TEMPLATE = 'daily_task_report'

#: Abort above this many recipients. The local database resolves 21; the EOD digest
#: resolved 60 active users on production in July 2026.
MAX_RECIPIENTS = 150

EMAIL, WHATSAPP = 'email', 'whatsapp'


def whatsapp_params(report, date_str, link):
    """The seven Interakt values, in the template's registered order: the header (the
    date), then six body values. Counts are strings, as Interakt takes them."""
    return [
        date_str,
        report['first_name'],
        report['scope'],
        str(report['delayed']),
        str(report['due_today']),
        str(report['no_due_date']),
        link,
    ]


def email_subject(report, date_str):
    return (f"Task report {date_str}: {report['delayed']} delayed, "
            f"{report['due_today']} due today")


class Command(BaseCommand):
    help = 'Send each person their morning task report (email and WhatsApp).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print recipients, scopes and counts. Sends and writes nothing.')
        parser.add_argument('--no-whatsapp', action='store_true',
                            help='Email only.')
        parser.add_argument('--only-user', type=str, default='', metavar='USERNAME',
                            help='Restrict to one recipient, by username.')
        parser.add_argument('--to', action='append', default=[], metavar='EMAIL',
                            help='With --only-user: send their email to these addresses '
                                 'instead (repeatable). No WhatsApp.')
        parser.add_argument('--i-am-sending-to-real-people', action='store_true',
                            help='Safety interlock. Required for a real send.')

    def handle(self, *args, **options):
        # Before anything else: which database is this run pointed at. HOST and NAME
        # only; the same dict holds PASSWORD, which is never printed.
        db_conf = settings.DATABASES.get('default', {})
        db_host = db_conf.get('HOST') or '(none - local/socket)'
        self.stdout.write(f"[db] host={db_host} name={db_conf.get('NAME') or '(unset)'}")

        dry_run = options['dry_run']
        only_user = options['only_user'].strip()
        to_emails = [e.strip() for e in options['to'] if e.strip()]
        armed = options['i_am_sending_to_real_people']
        channels_wanted = [EMAIL] if options['no_whatsapp'] else [EMAIL, WHATSAPP]

        if to_emails and not only_user:
            raise CommandError('--to needs --only-user: it sends one person\'s report.')

        report_date = timezone.localdate()  # IST calendar date (TIME_ZONE=Asia/Kolkata)
        date_str = report_date.strftime('%d %b %Y')
        # The same base-URL expression send_eod_digest uses; no setting of its own.
        app_url = getattr(settings, 'APP_BASE_URL',
                          'https://horizon-solar-pms-production.up.railway.app')

        reports = build_task_reports(report_date)
        if only_user:
            reports = [r for r in reports if r['profile'].user.username == only_user]
            if not reports:
                raise CommandError(
                    f'--only-user {only_user!r}: no such active user, or all three of '
                    'their counts are zero today.')

        # Circuit breaker, before any send. A dry run reports it instead of stopping,
        # so the operator can see who the oversized list is.
        too_many = len(reports) > MAX_RECIPIENTS
        if too_many and not dry_run:
            raise CommandError(
                f'ABORTED: {len(reports)} recipients is more than {MAX_RECIPIENTS}. '
                'Nothing was sent.')

        for report in reports:
            report['link'] = app_url.rstrip('/') + report['link_path']

        # --to: the one person's email, to the typed addresses only.
        if to_emails:
            self._send_to_addresses(reports[0], to_emails, date_str, dry_run)
            return

        # Which channels each recipient already has today. One query, read-only, so the
        # dry run shows exactly what a real run would skip. created_at__date resolves in
        # TIME_ZONE (IST) under USE_TZ, the same calendar day report_date is.
        # Not a lock: two runs started at the same moment could both send. The cron
        # fires once a day, which is the protection against that.
        already = defaultdict(set)
        for recipient_id, channel in NotificationLog.objects.filter(
            template_name=TEMPLATE, status='sent', created_at__date=report_date,
            recipient__in=[r['profile'] for r in reports],
        ).values_list('recipient_id', 'channel'):
            already[recipient_id].add(channel)

        plan = [(r, [c for c in channels_wanted if c not in already[r['profile'].pk]])
                for r in reports]

        if dry_run:
            # Render every report even though nothing is sent: a template error on real
            # data then shows up here, not halfway through the real send.
            for report, _ in plan:
                self._render(report, date_str)
            self._print_dry_run(plan, too_many)
            return

        # Safety interlock: a real send needs the flag. Placed after the recipients are
        # resolved so the refusal can name them, and before the first send.
        if not armed:
            self.stderr.write(
                'REFUSING TO SEND: --i-am-sending-to-real-people was not given.\n'
                f'  database host : {db_host}\n'
                f'  would send to : {len(plan)} recipient(s)')
            for report, channels in plan:
                self.stderr.write(f"    {report['profile'].user.username} "
                                  f"({report['role']}): {', '.join(channels) or 'nothing (already sent)'}")
            self.stderr.write('Re-run with --dry-run to test safely, or with '
                              '--i-am-sending-to-real-people for the real send.')
            sys.exit(1)

        sent_email = sent_whatsapp = skipped = 0
        for report, channels in plan:
            if not channels:
                skipped += 1
                self.stdout.write(f"[skip] {report['profile'].user.username}: already sent today")
                continue
            text_body, html_body = self._render(report, date_str)
            send_notification(
                report['profile'], text_body, channels=channels,
                subject=email_subject(report, date_str), html_message=html_body,
                template=TEMPLATE,
                template_params=whatsapp_params(report, date_str, report['link']),
            )
            sent_email += EMAIL in channels
            sent_whatsapp += WHATSAPP in channels
            self.stdout.write(f"[send] {report['profile'].user.username}: {', '.join(channels)}")
        self.stdout.write(
            f'Done. email attempts={sent_email} whatsapp attempts={sent_whatsapp} '
            f'skipped (already sent)={skipped}. Each attempt is in NotificationLog as '
            'sent / failed / skipped.')

    def _render(self, report, date_str):
        context = {'report': report, 'date_str': date_str, 'link': report['link']}
        return (render_to_string('projects/email/morning_task_report.txt', context),
                render_to_string('projects/email/morning_task_report.html', context))

    def _send_to_addresses(self, report, to_emails, date_str, dry_run):
        """Send one person's email version to typed addresses. log_recipient=None: the
        row would otherwise sit under that person's profile as 'sent' and the duplicate
        check would then skip their real report today."""
        text_body, html_body = self._render(report, date_str)
        subject = email_subject(report, date_str)
        for address in to_emails:
            if dry_run:
                self.stdout.write(f'[dry-run] would send {subject!r} to {address}')
                continue
            send_aggregate_email(to_email=address, subject=subject, text_body=text_body,
                                 html_body=html_body, log_recipient=None,
                                 template_name=TEMPLATE)
            self.stdout.write(f'[to] {address}: handed to send_aggregate_email '
                              '(application log only; master switch applies)')

    def _print_dry_run(self, plan, too_many):
        header = f"{'username':<16} {'role':<20} {'scope':<20} {'delayed':>7} {'today':>5} {'no date':>7}  send"
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        emails = whatsapps = 0
        largest = (0, '', '')
        for report, channels in plan:
            username = report['profile'].user.username
            self.stdout.write(
                f"{username:<16} {report['role']:<20} {report['scope']:<20} "
                f"{report['delayed']:>7} {report['due_today']:>5} {report['no_due_date']:>7}  "
                f"{', '.join(channels) or 'skip (already sent today)'}")
            emails += EMAIL in channels
            whatsapps += WHATSAPP in channels
            for key in ('delayed', 'due_today', 'no_due_date'):
                if report[key] > largest[0]:
                    largest = (report[key], username, key)
        self.stdout.write('-' * len(header))
        self.stdout.write(f'recipients={len(plan)} emails={emails} whatsapps={whatsapps} '
                          f'largest single count={largest[0]} ({largest[1]}, {largest[2]})')
        if too_many:
            self.stdout.write(f'WOULD ABORT: more than {MAX_RECIPIENTS} recipients.')
        self.stdout.write('[dry-run] nothing sent, nothing written.')
