"""
Management command: the daily Site Dashboard snapshot, emailed as PNG images (SNAP-1).

    python manage.py send_dashboard_snapshot                         # the 8 PM run
    python manage.py send_dashboard_snapshot --dry-run --out-dir DIR # files only
    python manage.py send_dashboard_snapshot --to USERNAME           # one person

DEFAULT RUN. Collect the figures (snapshot.collect_snapshot, the dashboard's own data
layer, read as the lowest-pk active CEO), save them as today's DashboardSnapshot row,
render the images with yesterday's row for the ▲/▼ arrows, and send the event
daily_dashboard_snapshot through notify_now() with the images attached. Recipients are the
event's Admin-named list; the code names nobody.

    Same IST date twice  today's row is OVERWRITTEN, never duplicated, and the output
                         says so. Tomorrow's arrows then compare against the last
                         figures sent.
    "Yesterday"          the newest row dated before today, so a missed day compares
                         against the last day that ran.

--dry-run   collect and render only: no row written, nothing sent. Needs --out-dir;
            prints each file's path and the raw figures as a table.
--to USER   send only to that active user, skipping the named list. The email master
            switch and the event's pause still apply: they are checked inside the
            chokepoint (send_notification / notify), which this does not go around.

EXIT CODE (Railway cron log, SNAP-1 ruling). 0 when sent, and 0 with one "skipped:"
line when nothing could go out by design (event paused, master switch off, the
person's email preference off, nobody on the named list). Non-zero, with one line
saying why, for a real error or any failed send.
"""
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from projects.models import DashboardSnapshot, NotificationLog, UserProfile
from projects.notify import notify_now
from projects.snapshot import (FIGURES, SnapshotError, collect_snapshot, headline,
                               render_snapshot_images)

EVENT = 'daily_dashboard_snapshot'
COMMAND_NAME = 'send_dashboard_snapshot'


class Command(BaseCommand):
    help = 'Email the daily Site Dashboard snapshot images (SNAP-1).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Collect and render only; write the PNGs to --out-dir. '
                                 'No database write, no send.')
        parser.add_argument('--out-dir', help='Folder to write the PNG files to.')
        parser.add_argument('--to', dest='to_username',
                            help='Send only to this user, not the named list.')

    def handle(self, *args, dry_run, out_dir, to_username, **options):
        # The Windows console cannot print '·' or '▲' in its default code page; a figure
        # table must never crash the run. Railway's Linux stdout is UTF-8 already.
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(errors='replace')
        if dry_run and not out_dir:
            raise CommandError('--dry-run needs --out-dir.')
        if dry_run and to_username:
            raise CommandError('--dry-run sends nothing; leave out --to.')
        try:
            self._run(dry_run, out_dir, to_username)
        except CommandError:
            raise
        except SnapshotError as exc:
            raise CommandError(f'snapshot not taken: {exc}') from exc
        except Exception as exc:
            # Anything else is a real error: one line for the cron log, non-zero exit.
            raise CommandError(f'snapshot failed: {type(exc).__name__}: {exc}') from exc

    # ------------------------------------------------------------------

    def _run(self, dry_run, out_dir, to_username):
        recipient = self._recipient(to_username) if to_username else None
        now = timezone.now()
        payload = collect_snapshot(now)
        today = payload['snapshot_date']
        # The newest row before today, so a missed day still gets arrows (module docstring).
        previous_row = (DashboardSnapshot.objects.filter(snapshot_date__lt=today)
                        .order_by('-snapshot_date').first())
        previous = previous_row.payload if previous_row else None
        images = render_snapshot_images(payload, previous)
        self.stdout.write(f'Collected {payload["taken_at_label"]} as {payload["viewer"]}; '
                          f'{len(images)} image(s); previous snapshot: '
                          f'{previous_row.snapshot_date if previous_row else "none"}.')

        if out_dir:
            self._write_files(out_dir, payload, images)
        if dry_run:
            self._print_figures(payload)
            self.stdout.write('Dry run: no row written, nothing sent.')
            return

        self._save(payload, now)
        self._send(payload, images, recipient)

    def _recipient(self, username):
        profile = (UserProfile.objects.select_related('user')
                   .filter(user__username=username).first())
        if profile is None:
            raise CommandError(f'--to: there is no user {username!r}.')
        # notify() drops a deactivated person silently; say so instead of "sent 0".
        if not (profile.is_active and profile.user.is_active):
            raise CommandError(f'--to: {username!r} is deactivated.')
        return profile

    def _save(self, payload, now):
        """Today's row, created or overwritten (one row per IST date)."""
        with transaction.atomic():
            # RACE: two runs on the same date (a manual --to while the cron runs) would
            # both try to create the row; update_or_create's select_for_update plus the
            # unique snapshot_date make the second one update instead of failing or
            # duplicating. The last writer's figures win, which is the intended result.
            row, created = DashboardSnapshot.objects.update_or_create(
                snapshot_date=payload['snapshot_date'],
                defaults={'taken_at': now, 'payload': payload,
                          'created_by_command': COMMAND_NAME})
        self.stdout.write(f'{"Saved" if created else "Overwrote"} the snapshot row for '
                          f'{row.snapshot_date} (#{row.pk}).')

    def _send(self, payload, images, recipient):
        date = payload['snapshot_date']
        attachments = [{'name': f'site-dashboard-{date}-{n}.png', 'mime_type': 'image/png',
                        'content': content} for n, content in enumerate(images, start=1)]
        values = {
            'snapshot_date': payload['snapshot_date_label'],
            'taken_at': payload['taken_at_label'],
            'headline': headline(payload),
            'image_count': f'{len(images)} image{"s" if len(images) != 1 else ""}',
        }
        # The rows this send writes are the ones after this pk; read back below for the
        # outcome, because notify_now() never raises once the event code is known.
        before = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        extra = {'named_recipients': False} if recipient else {}
        # notify_now(), not notify(): there is no action here to wait for. The snapshot
        # row is already committed (its own atomic block above) and nothing else is open,
        # so notify() would only register an on_commit callback that runs at once. Calling
        # notify_now() directly says so, and the event (not deferred) has finished
        # sending when it returns, so the rows read below are the outcome.
        notify_now(EVENT, recipients=[recipient] if recipient else [], values=values,
                   link=reverse('site_dashboard'), log_label=EVENT, attachments=attachments,
                   **extra)
        self._report(before)

    def _report(self, before):
        rows = list(NotificationLog.objects.filter(pk__gt=before, event_code=EVENT,
                                                   channel='email')
                    .values_list('status', 'error_detail', 'recipient__user__username'))
        for status, detail, username in rows:
            self.stdout.write(f'  email {status:8} {username}{": " + detail if detail else ""}')
        failed = [r for r in rows if r[0] == 'failed']
        if failed:
            raise CommandError(f'{len(failed)} of {len(rows)} email(s) failed: '
                               f'{failed[0][1] or "no detail"}')
        sent = sum(1 for r in rows if r[0] == 'sent')
        if sent:
            self.stdout.write(f'Sent {sent} email(s).')
            return
        if not rows:
            self.stdout.write('skipped: nobody to send to (the named list is empty).')
            return
        reasons = sorted({detail or status for status, detail, _ in rows})
        self.stdout.write(f'skipped: {"; ".join(reasons)}.')

    def _write_files(self, out_dir, payload, images):
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for n, content in enumerate(images, start=1):
            path = (folder / f'site-dashboard-{payload["snapshot_date"]}-{n}.png').resolve()
            path.write_bytes(content)
            self.stdout.write(f'Wrote {path} ({len(content):,} bytes)')

    def _print_figures(self, payload):
        """The raw figures as a table: the organisation row, every PM, every tender."""
        short = ['Sites', 'DC kWp', 'Behind', 'Civil', 'Overdue', 'Blocked', 'Issues', 'Quiet']
        self.stdout.write('')
        self.stdout.write('Organisation (' + payload['dates_as_of']['text'] + ')')
        for tile in payload['org']['tiles']:
            self.stdout.write(f'  {tile["label"]:22} {str(tile["value"]):>8}   '
                              f'{tile["detail"] or ""}')
        for title, rows in (('By PM', payload['pms']), ('By tender', payload['tenders'])):
            self.stdout.write('')
            self.stdout.write(f'{title:34}' + ''.join(f'{h:>9}' for h in short))
            for row in rows:
                self.stdout.write(f'{row["name"][:34]:34}' + ''.join(
                    f'{str(row["values"][key]):>9}' for key, _, _ in FIGURES))
