"""
Management command: the daily Site Dashboard snapshot, emailed as PNG images (SNAP-1;
one section per tender since SNAP-1b).

    python manage.py send_dashboard_snapshot                         # the 8 PM run
    python manage.py send_dashboard_snapshot --dry-run --out-dir DIR # files only
    python manage.py send_dashboard_snapshot --to USERNAME           # one person

DEFAULT RUN. Collect the figures (snapshot.collect_snapshot, the dashboard's own data
layer, read as the lowest-pk active CEO), save them as today's DashboardSnapshot row,
render the images with yesterday's row for the ▲/▼ arrows, and send the event
daily_dashboard_snapshot through notify_now() with the images attached. Recipients are the
event's Admin-named list; the code names nobody.

    No tender with a site in execution   the row is still saved, nothing is sent, and
                                         the one line is snapshot.NO_TENDER_SKIP, exit 0
                                         (Zuber, 14 Nov).

    Same IST date twice  today's row is OVERWRITTEN, never duplicated, and the output
                         says so. Tomorrow's arrows then compare against the last
                         figures sent.
    "Yesterday"          the newest row dated before today, so a missed day compares
                         against the last day that ran.

--dry-run   collect and render only: no row written, nothing sent. Needs --out-dir;
            prints each file's path and, per tender, the strip, the tiles and their
            sub-lines.
--to USER   send only to that active user, skipping the named list. The email master
            switch and the event's pause still apply: they are checked inside the
            chokepoint (send_notification / notify), which this does not go around.

WHATSAPP (SNAP-2). The same event also sends the Interakt template daily_snapshot_pm to
each recipient: its header is one image, its body the collection time as the image
prints it. Each PNG is uploaded to the PRIVATE design bucket at
snapshots/<IST date>/site-dashboard-<IST date>-<n>-<HHMMSS>.png (a new object every
run, never overwritten) and a link signed for SNAPSHOT_LINK_SECONDS (7 days) is the
header; one message per image per recipient, in order. Nothing is uploaded while the
event's WhatsApp is paused or the master switch is off. The log rows name the storage
path, never the link. Email is unchanged.

--dry-run   also prints, per named recipient, the WhatsApp that would be sent (template,
            "signed URL for <path>", the body value). Nothing is uploaded.

EXIT CODE (Railway cron log, SNAP-1 ruling). 0 when sent, and 0 with one "skipped:"
line per channel when nothing could go out by design (event paused, master switch off,
the person's preference off, nobody on the named list). Non-zero, with one line per
failure saying why, for a real error or any failed send on either channel (SNAP-2 W7).
"""
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from projects.design_storage import (DesignStorageError, get_design_file_url,
                                     upload_private_bytes)
from projects.models import DashboardSnapshot, NotificationLog, SystemSettings, UserProfile
from projects.notifications import NO_VALID_WHATSAPP_NUMBER, valid_whatsapp_number
from projects.notify import channel_is_active, merge_named_recipients, notify_now
from projects.notify_registry import WHATSAPP, get_event
from projects.snapshot import (NO_TENDER_SKIP, STRIP, SnapshotError, collect_snapshot,
                               delta_text, headline, ordered_sections, pm_line,
                               render_snapshot_images, section_deltas, section_title)
from projects.whatsapp_params import sanitise_params

EVENT = 'daily_dashboard_snapshot'
COMMAND_NAME = 'send_dashboard_snapshot'

# W3: how long the WhatsApp image link works. Interakt fetches the image when it sends,
# and a recipient may open the message days later; a week covers both, and the link
# then dies, so a forwarded copy of it does not open the bucket for good.
SNAPSHOT_LINK_SECONDS = 7 * 24 * 60 * 60


def snapshot_object_path(moment, number, name='site-dashboard'):
    """The bucket path of one image, from the IST collection moment: a new object every
    run (the time is in the name), so a same-day re-run never collides with or replaces
    an image already sent."""
    local = timezone.localtime(moment)
    day = f'{local:%Y-%m-%d}'
    return f'snapshots/{day}/{name}-{day}-{number}-{local:%H%M%S}.png'


def upload_snapshot_image(path, content):
    """Upload one PNG and sign it: {'path', 'url', 'error'}. Never raises; a failure is
    the plain reason in 'error' (url None), so the caller still sends the email and the
    WhatsApp leg writes a 'failed' row naming it. Shared with test_whatsapp --image."""
    try:
        bucket, path = upload_private_bytes(path, content, 'image/png')
        url = get_design_file_url(bucket, path, SNAPSHOT_LINK_SECONDS)
    except DesignStorageError as exc:
        return {'path': path, 'url': None, 'error': str(exc)}
    if not url:
        return {'path': path, 'url': None, 'error': 'storage returned no signed link'}
    return {'path': path, 'url': url, 'error': ''}


def whatsapp_will_send(event):
    """Whether this run's WhatsApp can go out at all: the event's channel switched on by
    Admin and the master switch on. When not, nothing is uploaded (Zuber, 15 Nov) and
    the leg writes its 'skipped' rows as usual."""
    return channel_is_active(event, WHATSAPP) and SystemSettings.get().whatsapp_enabled


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
                          f'{len(payload["tender_sections"])} tender(s), '
                          f'{len(images)} image(s); previous snapshot: '
                          f'{previous_row.snapshot_date if previous_row else "none"}.')

        if out_dir:
            self._write_files(out_dir, payload, images)
        if dry_run:
            self._print_figures(payload, previous)
            if not images:
                self.stdout.write(NO_TENDER_SKIP)
            else:
                self._print_whatsapp(payload, images, now)
                self.stdout.write('Dry run: nothing uploaded.')
            self.stdout.write('Dry run: no row written, nothing sent.')
            return

        # The row is saved even when nothing is drawn, so the day keeps its record
        # (Zuber, 14 Nov); tomorrow's arrows then find no section for a tender that had
        # none today and draw nothing for it.
        self._save(payload, now)
        if not images:
            self.stdout.write(NO_TENDER_SKIP)
            return
        self._send(payload, images, recipient, now)

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

    def _send(self, payload, images, recipient, now):
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
        # SNAP-2: upload only when the WhatsApp can go out; otherwise pass no images and
        # the leg writes its 'skipped' rows (paused, or master switch off).
        if whatsapp_will_send(get_event(EVENT)):
            extra['whatsapp_images'] = self._upload(images, now)
        # notify_now(), not notify(): there is no action here to wait for. The snapshot
        # row is already committed (its own atomic block above) and nothing else is open,
        # so notify() would only register an on_commit callback that runs at once. Calling
        # notify_now() directly says so, and the event (not deferred; its WhatsApp sent on
        # this thread because it is command_only) has finished sending when it returns,
        # so the rows read below are the outcome. notify() refuses this event anyway.
        notify_now(EVENT, recipients=[recipient] if recipient else [], values=values,
                   link=reverse('site_dashboard'), log_label=EVENT, attachments=attachments,
                   **extra)
        self._report(before)

    def _upload(self, images, now):
        """Each PNG to the private bucket with a signed link, in image order. A failed
        upload is kept in the list with its reason, so every recipient gets a 'failed'
        row for that image and the run exits non-zero; the email is not held back."""
        uploaded = []
        for number, content in enumerate(images, start=1):
            image = upload_snapshot_image(snapshot_object_path(now, number), content)
            uploaded.append(image)
            if image['error']:
                self.stdout.write(f'Upload failed for {image["path"]}: {image["error"]}')
            else:
                self.stdout.write(f'Uploaded {image["path"]} (link valid 7 days).')
        return uploaded

    def _report(self, before):
        # The rows this run wrote on the two external channels, in the order written.
        rows = list(NotificationLog.objects.filter(pk__gt=before, event_code=EVENT,
                                                   channel__in=('email', WHATSAPP))
                    .order_by('pk')
                    .values_list('channel', 'status', 'error_detail',
                                 'recipient__user__username'))
        for channel, status, detail, username in rows:
            self.stdout.write(f'  {channel:8} {status:8} {username}'
                              f'{": " + detail if detail else ""}')
        failed = [r for r in rows if r[1] == 'failed']
        if failed:
            # W7: one line per failure, so the cron log says which send failed and why.
            lines = [f'{channel} to {username} failed: {detail or "no detail"}'
                     for channel, _, detail, username in failed]
            raise CommandError(f'{len(failed)} of {len(rows)} send(s) failed.\n'
                               + '\n'.join(lines))
        if not rows:
            self.stdout.write('skipped: nobody to send to (the named list is empty).')
            return
        for channel, noun, title in (('email', 'email(s)', 'Email'),
                                     (WHATSAPP, 'WhatsApp message(s)', 'WhatsApp')):
            mine = [r for r in rows if r[0] == channel]
            sent = sum(1 for r in mine if r[1] == 'sent')
            if sent:
                self.stdout.write(f'Sent {sent} {noun}.')
            elif mine:
                reasons = sorted({detail or status for _, status, detail, _ in mine})
                self.stdout.write(f'{title} skipped: {"; ".join(reasons)}.')

    def _print_whatsapp(self, payload, images, now):
        """The dry run's WhatsApp: per named recipient, what would be sent. Reads the
        list and the switches only; uploads nothing, signs nothing."""
        event = get_event(EVENT)
        spec = event.whatsapp
        # The body exactly as a real send would clean it; the header slot is left empty
        # because no link is signed in a dry run.
        body = sanitise_params(['', *spec.params({'taken_at': payload['taken_at_label']})],
                               body_fixed_chars=spec.body_fixed_chars, media_header=True)[1:]
        self.stdout.write('')
        self.stdout.write(f'WhatsApp: channel {"active" if channel_is_active(event, WHATSAPP) else "paused"}, '
                          f'master switch {"ON" if SystemSettings.get().whatsapp_enabled else "OFF"}; '
                          f'template {spec.template} ({spec.language}), '
                          f'{len(images)} message(s) per recipient')
        people, inactive = merge_named_recipients(event, [])
        if not people and not inactive:
            self.stdout.write('  nobody on the named list')
        for profile in inactive:
            self.stdout.write(f'  {profile.user.username}: inactive, nothing sent')
        for profile in people:
            name = profile.user.username
            if valid_whatsapp_number(profile.phone_number) is None:
                self.stdout.write(f'  {name}: {NO_VALID_WHATSAPP_NUMBER} (would fail)')
                continue
            for number in range(1, len(images) + 1):
                self.stdout.write(f'  {name} #{number}: header: signed URL for '
                                  f'{snapshot_object_path(now, number)} | body {{{{1}}}}: '
                                  f'{body[0]!r}')

    def _write_files(self, out_dir, payload, images):
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for n, content in enumerate(images, start=1):
            path = (folder / f'site-dashboard-{payload["snapshot_date"]}-{n}.png').resolve()
            path.write_bytes(content)
            self.stdout.write(f'Wrote {path} ({len(content):,} bytes)')

    def _print_figures(self, payload, previous):
        """What the images draw, as text: per tender in drawing order, the heading, the
        strip and the 7 tiles with their sub-lines and arrows."""
        self.stdout.write('')
        self.stdout.write(payload['dates_as_of']['text'])
        for section in ordered_sections(payload):
            deltas = section_deltas(section, previous)
            code, name = section_title(section)
            self.stdout.write('')
            self.stdout.write(f'{code}{" · " + name if name else ""} (Program '
                              f'{section["id"]}) | {pm_line(section)}')
            strip = []
            for key, label, _ in STRIP:
                arrow = delta_text(deltas[key])
                strip.append(f'{label} {section["breakdown"][key]}'
                             f'{" (" + arrow + ")" if arrow else ""}')
            self.stdout.write('  ' + ' › '.join(strip[:3]) + ' | ' + strip[3])
            for tile in section['tiles']:
                arrow = delta_text(deltas[tile['key']])
                self.stdout.write(f'  {tile["label"]:22} {str(tile["value"]):>8} '
                                  f'{arrow:>7}   {tile["detail"] or ""}')
