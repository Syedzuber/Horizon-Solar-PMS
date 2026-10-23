"""
Management command: give every email notification event an ACTIVE version 1 holding the
wording that is in code today (T3a).

    python manage.py seed_notification_templates --as <ADMIN_USERNAME> --dry-run
    python manage.py seed_notification_templates --as <ADMIN_USERNAME>
    python manage.py seed_notification_templates --as <ADMIN_USERNAME> --event issue_raised

WHY: until an event has a version, notify() renders the registry's code default and the
Admin screens show "code default" with nothing to start a history from. Seeding version 1
makes the wording that is going out a numbered, attributed version, so Admin's first edit
is version 2 and the history shows what it replaced.

THE ONE EXCEPTION TO TEST-BEFORE-ACTIVATE. Every other route to an active version needs a
test send after the last save (T2, A6). This command activates without one, through
notify_admin.seed_initial_version(), because what it activates is the code default that
was already being sent, proven word for word by tests_notify_migration_a. Nothing changes
for a recipient on the day it runs.

IDEMPOTENT: an event that already has any email version, of any status, is skipped and
listed, so a second run creates nothing and a run after Admin has started editing never
puts the code default back. --dry-run prints what it would create and writes nothing.

WHO: --as must name an active Admin (role 'Admin', ruling T-1; System Admin is not one).
The versions are created in their name and each event gets one ActivityLog line, with no
project, recording who seeded it.
"""

from django.core.management.base import BaseCommand, CommandError

from projects.models import NOTIFY_CHANNEL_EMAIL, NotificationTemplate, log_activity
from projects.notify_admin import NotificationAdminRefused, seed_initial_version
from projects.notify_registry import NOTIFICATION_EVENTS
from projects.permissions import active_admins


class Command(BaseCommand):
    help = ('Create an ACTIVE version 1 from the code default for every email '
            'notification event that has no version yet.')

    def add_arguments(self, parser):
        parser.add_argument('--as', dest='as_username', required=True,
                            help='Username of the active Admin the versions are created by.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be created; write nothing.')
        parser.add_argument('--event', dest='event_code',
                            help='Seed only this event code.')

    def handle(self, *args, as_username, dry_run, event_code, **options):
        # Admin only, and active: the same people who may author these versions on the
        # Admin screens (notify_views admits role 'Admin' only). Anyone else, System Admin
        # included, is refused before anything is read or written.
        admin = (active_admins().select_related('user')
                 .filter(user__username=as_username).first())
        if admin is None:
            raise CommandError(f'{as_username!r} is not an active Admin; only an active '
                               f'Admin may seed notification templates.')

        # Every event that declares email, in registry order; --event narrows it to one,
        # and an unknown or non-email code is refused rather than silently doing nothing.
        events = [e for e in NOTIFICATION_EVENTS.values() if NOTIFY_CHANNEL_EMAIL in e.channels]
        if event_code:
            events = [e for e in events if e.code == event_code]
            if not events:
                raise CommandError(f'{event_code!r} is not an email notification event.')

        prefix = '[dry run] ' if dry_run else ''
        created, skipped = [], []
        for event in events:
            existing = NotificationTemplate.objects.filter(
                event_code=event.code, channel=NOTIFY_CHANNEL_EMAIL).count()
            if existing:
                skipped.append(event)
                self.stdout.write(f'{prefix}skip    {event.code}: already has {existing} '
                                  f'email version(s)')
                continue
            if dry_run:
                created.append(event)
                self.stdout.write(f'{prefix}create  {event.code}: version 1, active, '
                                  f'subject "{event.default_subject}"')
                continue
            try:
                template = seed_initial_version(event.code, NOTIFY_CHANNEL_EMAIL, admin)
            except NotificationAdminRefused as exc:
                # Another run, or an Admin, created a version between the count above and
                # this call; seed_initial_version re-checks inside its transaction.
                skipped.append(event)
                self.stdout.write(f'skip    {event.code}: {exc}')
                continue
            log_activity(
                None, admin,
                f'Seeded notification template "{event.label}" (email) version 1 from the '
                f'code default',
                entity_type='NotificationTemplate', entity_id=template.pk,
                action_code='notify_template_seeded',
            )
            created.append(event)
            self.stdout.write(f'create  {event.code}: version 1 active (#{template.pk})')

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{verb} {len(created)} version(s); skipped {len(skipped)}.'))
