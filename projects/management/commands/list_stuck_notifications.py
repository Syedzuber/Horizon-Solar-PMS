"""List deferred notification sends stuck in 'pending' (NTF-DCG S2, S7). Read-only.

    python manage.py list_stuck_notifications [--minutes 10]

WHAT A STUCK ROW IS. notify_deferred writes a 'pending' NotificationLog row when it
hands a send to the background pool, and the job moves it to sent / failed / skipped
within seconds (each provider call times out at 10 s). A row still pending after
--minutes was lost: the gunicorn worker was killed or restarted (a redeploy, a crash,
a worker timeout) before the job finished, or the job never got a thread.

IT RETRIES NOTHING (S7). A WhatsApp may have gone out before the crash, so resending
could message a person twice. A person decides, from the event, recipient and time
printed here, whether to send again by hand.

READ-ONLY: one SELECT, nothing logged, no SystemSettings read. Prints the database host
and name first, never the password.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from projects.models import NotificationLog

DEFAULT_MINUTES = 10


class Command(BaseCommand):
    help = "Print deferred notification sends still 'pending' after N minutes. Read-only."

    def add_arguments(self, parser):
        parser.add_argument('--minutes', type=int, default=DEFAULT_MINUTES,
                            help=f'Age in minutes before a pending row counts as stuck '
                                 f'(default {DEFAULT_MINUTES}).')

    def handle(self, *args, **options):
        minutes = options['minutes']
        if minutes < 0:
            raise CommandError('--minutes must be 0 or more.')
        db = settings.DATABASES['default']
        w = self.stdout.write
        w(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")

        now = timezone.now()
        cutoff = now - timedelta(minutes=minutes)
        # Pending rows older than the cutoff, oldest first, with the recipient's user
        # joined so the listing reads names without a query per row. created_at is the
        # hand-off time: a deferred row is written as it is handed to the pool.
        rows = list(NotificationLog.objects
                    .filter(status='pending', created_at__lt=cutoff)
                    .select_related('recipient__user')
                    .order_by('created_at', 'pk'))

        w(f'Pending for more than {minutes} min (before '
          f'{timezone.localtime(cutoff):%Y-%m-%d %H:%M:%S %Z}): {len(rows)}')
        for row in rows:
            user = row.recipient.user
            age = int((now - row.created_at).total_seconds() // 60)
            w(f'  #{row.pk}  {timezone.localtime(row.created_at):%Y-%m-%d %H:%M:%S}  '
              f'age {age} min  {row.channel}  event={row.event_code or "-"}  '
              f'label={row.template_name or "-"}  to={user.get_full_name() or user.username} '
              f'({user.username})')
