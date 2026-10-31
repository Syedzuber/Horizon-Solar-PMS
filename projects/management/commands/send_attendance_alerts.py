"""
Contractor attendance "not logged" alert, to each site's PM and to SCM (ATT-B4).

Run once a morning. The time it runs is the cutoff the message names: the intended
schedule is 10:30 AM IST, a Railway cron entry `0 5 * * *` (UTC), set on Railway and
not recorded in this repo.

    python manage.py send_attendance_alerts --dry-run
    python manage.py send_attendance_alerts --i-am-sending-to-real-people

WHAT IS SENT. Which sites are missing attendance and who hears about which of them come
from projects.attendance_alerts; this command only decides channels and sends:
  * in-app and email through notify() (event 'attendance_missing'), so the event's Admin
    switch, the master switch, each person's preference and NotificationLog all apply.
    The event starts paused: while it is, email is skipped and in-app still goes, which
    is how notify() treats every paused event (SECONDARY_FINDINGS, ATT-B4).
  * WhatsApp through send_notification() directly, with the Interakt template
    'contractor_attendance_missing' and four parameters: the header (the date), then the
    first name, the cutoff and the site list. A DELIBERATE EXCEPTION to "everything
    through notify()" (ruling R1), as send_morning_task_report: notify() has no WhatsApp
    leg yet. Gated by settings.ATTENDANCE_ALERT_WHATSAPP_ENABLED, not by Admin. Somebody
    with no phone number is not sent one.

Nothing missing, nothing sent (ruling 8). A weekday in ATTENDANCE_ALERT_SKIP_WEEKDAYS
(Sunday by default) sends nothing and says why.

IDEMPOTENT PER CHANNEL (ruling R2). A channel is skipped for a person who already has a
'sent' row for this event on it today (IST). notify() sends in-app and email together, so
it is called when in-app has not gone yet, or when in-app has gone but an email that can
now be delivered has not; the second case repeats the in-app message, which only happens
when email was switched on between two runs on one day.

Options:
  --dry-run          print each recipient's role, the channels a real run would use, the
                     site list and what the log already shows today. Sends nothing and
                     writes nothing; needs no interlock flag.
  --date YYYY-MM-DD  check that day instead of today (IST). For testing.
  --i-am-sending-to-real-people
                     safety interlock. Without it (and without --dry-run) the command
                     names the recipients it resolved and exits 1.

CIRCUIT BREAKER. More than MAX_RECIPIENTS recipients aborts before anything is sent.

Every run prints DATABASES['default'] HOST and NAME first (never the password).
"""
import sys
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from projects.attendance_alerts import (
    EVENT_CODE, WHATSAPP_TEMPLATE, channels_sent_today, first_name, format_site_lines,
    format_site_list, group_by_recipient, sites_missing_attendance, sites_without_pm,
)
from projects.decorators import get_user_dashboard
from projects.models import SystemSettings
from projects.notifications import send_notification
from projects.notify import (
    RenderError, active_template, channel_is_active, notify, render_email,
)
from projects.notify_registry import get_event

#: Abort above this many recipients: several times the number of PMs and SCM users the
#: company has, so it means a selection or recipient bug.
MAX_RECIPIENTS = 150

IN_APP, EMAIL, WHATSAPP = 'in_app', 'email', 'whatsapp'

WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
                 'Sunday')


def date_label(day):
    """'31 Oct' — the WhatsApp header and the email subject. Built from day and %b
    because %-d does not exist on Windows."""
    return f'{day.day} {day:%b}'


def cutoff_label(moment):
    """'10:30 AM' from the IST run time (ruling 2)."""
    return moment.strftime('%I:%M %p').lstrip('0')


def whatsapp_params(day_str, profile, cutoff, site_list):
    """The four Interakt values in the template's registered order: header {{1}} the date,
    then body {{1}} first name, {{2}} cutoff, {{3}} site list. None holds a line break."""
    return [day_str, first_name(profile), cutoff, site_list]


def whatsapp_text(profile, cutoff, site_list):
    """The WhatsApp body as the template words it, for the log row's message."""
    return (f'Hello {first_name(profile)}, by {cutoff} today no contractor attendance has '
            f'been logged on these sites with installation work in progress: {site_list}. '
            'Please follow up with the site engineers and get the crews logged in Horizon PMS.')


class Command(BaseCommand):
    help = 'Tell PMs and SCM which sites have installation in progress but no attendance today.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be sent. Sends and writes nothing.')
        parser.add_argument('--date', type=str, default='', metavar='YYYY-MM-DD',
                            help='Check this day instead of today (IST). For testing.')
        parser.add_argument('--i-am-sending-to-real-people', action='store_true',
                            help='Required for a real send.')

    def handle(self, *args, **options):
        # Before anything else: which database is this run pointed at. HOST and NAME
        # only; the same dict holds PASSWORD, which is never printed.
        db_conf = settings.DATABASES.get('default', {})
        db_host = db_conf.get('HOST') or '(none - local/socket)'
        self.stdout.write(f"[db] host={db_host} name={db_conf.get('NAME') or '(unset)'}")

        dry_run = options['dry_run']
        armed = options['i_am_sending_to_real_people']
        if options['date']:
            try:
                day = date.fromisoformat(options['date'])
            except ValueError:
                raise CommandError(f"--date {options['date']!r} is not YYYY-MM-DD.") from None
        else:
            day = timezone.localdate()  # IST calendar date (TIME_ZONE=Asia/Kolkata)
        cutoff = cutoff_label(timezone.localtime())
        day_str = date_label(day)
        self.stdout.write(f'[date] {day.isoformat()} ({WEEKDAY_NAMES[day.weekday()]}), '
                          f'cutoff {cutoff}')

        if day.weekday() in settings.ATTENDANCE_ALERT_SKIP_WEEKDAYS:
            self.stdout.write(f'Skipped: {WEEKDAY_NAMES[day.weekday()]} is in '
                              'ATTENDANCE_ALERT_SKIP_WEEKDAYS. Nothing sent.')
            return

        sites = sites_missing_attendance(day)
        if not sites:
            self.stdout.write('No site is missing attendance. Nothing sent.')
            return
        self.stdout.write(f'[sites] {len(sites)} missing attendance: '
                          f'{format_site_list(sites, limit=len(sites))}')
        for site in sites_without_pm(sites):
            self.stdout.write(f'[gap] {site.project_id}: no active assigned PM; SCM is still told')

        groups = group_by_recipient(sites)
        too_many = len(groups) > MAX_RECIPIENTS
        if too_many and not dry_run:
            raise CommandError(f'ABORTED: {len(groups)} recipients is more than '
                               f'{MAX_RECIPIENTS}. Nothing was sent.')

        plan = self._plan(day, groups)

        if dry_run:
            self._check_render(plan, day_str, cutoff)
            self._print_plan(plan, too_many)
            return

        # Safety interlock: placed after the recipients are resolved so the refusal can
        # name them, and before the first send.
        if not armed:
            self.stderr.write(
                'REFUSING TO SEND: --i-am-sending-to-real-people was not given.\n'
                f'  database host : {db_host}\n'
                f'  would send to : {len(plan)} recipient(s)')
            for row in plan:
                self.stderr.write(f"    {row['profile'].user.username} ({row['role']}): "
                                  f"{', '.join(row['channels']) or 'nothing'}")
            self.stderr.write('Re-run with --dry-run to test safely, or with '
                              '--i-am-sending-to-real-people for the real send.')
            sys.exit(1)

        self._send(plan, day_str, cutoff, len(sites))

    # ---- planning -----------------------------------------------------------------

    def _plan(self, day, groups):
        """One row per recipient: what was already sent today, and which channels a real
        run uses now. Reads only; a fixed number of queries whatever the site count."""
        event = get_event(EVENT_CODE)
        email_on = channel_is_active(event, EMAIL)
        try:
            email_master = SystemSettings.get().email_enabled
        except Exception:
            email_master = False
        whatsapp_on = settings.ATTENDANCE_ALERT_WHATSAPP_ENABLED
        already = channels_sent_today(day, [profile for profile, _ in groups])

        plan = []
        for (profile, role), sites in groups.items():
            done = set(already.get(profile.pk, ()))
            # Whether an email would really go out now: the same three gates
            # send_notification() applies, plus an address. Only used to decide whether
            # a repeat notify() is worth its repeated in-app message.
            email_deliverable = (email_on and email_master and profile.email_notifications
                                 and bool((profile.user.email or '').strip()))
            notes = []
            channels = []
            if IN_APP not in done or (EMAIL not in done and email_deliverable):
                channels.append(IN_APP)
                if EMAIL not in done and email_deliverable:
                    channels.append(EMAIL)
            if EMAIL in done:
                pass
            elif not email_on:
                notes.append('email paused')
            elif not email_deliverable:
                notes.append('email not deliverable (master switch, preference or address)')
            if WHATSAPP in done:
                pass
            elif not whatsapp_on:
                notes.append('whatsapp off (ATTENDANCE_ALERT_WHATSAPP_ENABLED)')
            elif not (profile.phone_number or '').strip():
                notes.append('whatsapp: no phone')
            else:
                channels.append(WHATSAPP)
            plan.append({'profile': profile, 'role': role, 'sites': sites, 'done': done,
                         'channels': channels, 'notes': notes})
        return plan

    def _check_render(self, plan, day_str, cutoff):
        """Render every email a real run would send, so a wording fault shows in the dry
        run. One template lookup for all of them; nothing is sent or written."""
        event = get_event(EVENT_CODE)
        template = active_template(EVENT_CODE, EMAIL)
        for row in plan:
            name = row['profile'].user.get_full_name() or row['profile'].user.username
            try:
                render_email(event, template, dict(self._values(row, day_str, cutoff),
                                                   recipient_name=name),
                             get_user_dashboard(row['profile'].user))
            except RenderError as exc:
                raise CommandError(f'The email wording does not render: {exc}') from None

    def _print_plan(self, plan, too_many):
        self.stdout.write(f'[dry-run] {len(plan)} recipient(s). Nothing sent, nothing written.')
        if too_many:
            self.stdout.write(f'[dry-run] WOULD ABORT: more than {MAX_RECIPIENTS} recipients.')
        for row in plan:
            profile = row['profile']
            done = ', '.join(sorted(row['done'])) or 'nothing'
            self.stdout.write(
                f"  {profile.user.username} ({row['role']}): "
                f"channels {', '.join(row['channels']) or 'none'}"
                f"{' [' + '; '.join(row['notes']) + ']' if row['notes'] else ''}; "
                f"already sent today: {done}; "
                f"sites: {format_site_list(row['sites'])}")

    # ---- sending ------------------------------------------------------------------

    def _values(self, row, day_str, cutoff):
        return {'date': day_str, 'cutoff': cutoff,
                'sites': format_site_list(row['sites']),
                'site_lines': format_site_lines(row['sites'])}

    def _send(self, plan, day_str, cutoff, site_count):
        counts = {'notify': 0, 'whatsapp': 0, 'already': 0, 'paused': 0, 'no_phone': 0,
                  'failed': 0}
        for row in plan:
            profile, channels = row['profile'], row['channels']
            counts['paused'] += 'email paused' in row['notes']
            counts['no_phone'] += 'whatsapp: no phone' in row['notes']
            if not channels:
                counts['already'] += 1
                self.stdout.write(f"[skip] {profile.user.username} ({row['role']}): "
                                  'already sent today')
                continue
            # One person's failure must not cost the next person their message: each leg
            # is caught, reported and the loop goes on. notify() and send_notification()
            # already log their own failures; this catches anything around them.
            try:
                if IN_APP in channels:
                    # actor=None: nobody caused this; the run is not a person, and
                    # notify() would otherwise drop the actor from the recipients.
                    notify(EVENT_CODE, recipients=[profile],
                           values=self._values(row, day_str, cutoff),
                           link=get_user_dashboard(profile.user), log_label=EVENT_CODE)
                    counts['notify'] += 1
                if WHATSAPP in channels:
                    site_list = format_site_list(row['sites'])
                    send_notification(
                        profile, whatsapp_text(profile, cutoff, site_list),
                        channels=[WHATSAPP], template=WHATSAPP_TEMPLATE,
                        template_params=whatsapp_params(day_str, profile, cutoff, site_list),
                        event_code=EVENT_CODE,
                    )
                    counts['whatsapp'] += 1
            except Exception as exc:
                counts['failed'] += 1
                self.stderr.write(f"[fail] {profile.user.username} ({row['role']}): "
                                  f'{type(exc).__name__}: {exc}')
                continue
            note = f" [{'; '.join(row['notes'])}]" if row['notes'] else ''
            self.stdout.write(f"[send] {profile.user.username} ({row['role']}): "
                              f"{', '.join(channels)}{note}")
        self.stdout.write(
            f"Done. sites missing={site_count} recipients={len(plan)} "
            f"in-app/email sends={counts['notify']} whatsapp attempts={counts['whatsapp']} "
            f"skipped (already sent)={counts['already']} email paused={counts['paused']} "
            f"no phone={counts['no_phone']} failed={counts['failed']}. Each attempt is in "
            'NotificationLog as sent / failed / skipped.')
