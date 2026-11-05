"""
Management command: name people to receive one notification event (NTF-DCG 5b, N5).

    python manage.py seed_named_recipients --as <ADMIN_USERNAME> --event CODE \
        --user USERNAME [--user USERNAME ...] --reason "..."            # dry run
    python manage.py seed_named_recipients ... --apply                  # writes

WHY: a later session opts an event in to named recipients and needs its first people
on the list on production without anyone clicking through the Admin screen. Every row
goes through notify_admin.add_named_recipient(), the same function the screen uses, so
the rules (event must opt in, person active, reason of 10+ characters) and the
ActivityLog line are identical.

DRY RUN BY DEFAULT: without --apply nothing is written; each user is reported as
"would add", "already active" or refused.

IDEMPOTENT: a person who is already an ACTIVE named recipient of the event is reported
and skipped, never added twice. Someone removed earlier is added again as a new row
(the removed row stays as history, N1).

WHO: --as must name an active Admin (role 'Admin', ruling T-1; System Admin is not one),
the people who may add named recipients on the Admin screen. The rows record them as
added_by. Not in the session prompt's signature; needed because added_by is mandatory.

REFUSED BEFORE ANYTHING IS READ: an unknown event, or one whose registry entry does not
set named_recipients=True (N2).
"""

from django.core.management.base import BaseCommand, CommandError

from projects.models import UserProfile
from projects.notify_admin import (
    NotificationAdminRefused, active_named_recipient, add_named_recipient,
)
from projects.notify_registry import NOTIFICATION_EVENTS
from projects.permissions import active_admins


class Command(BaseCommand):
    help = 'Add named recipients to a notification event that opts in (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--as', dest='as_username', required=True,
                            help='Username of the active Admin the rows are added by.')
        parser.add_argument('--event', dest='event_code', required=True,
                            help='Notification event code; it must take named recipients.')
        parser.add_argument('--user', dest='usernames', action='append', required=True,
                            help='Username to add; repeat for several people.')
        parser.add_argument('--reason', required=True,
                            help='Why these people receive the event (10+ characters).')
        parser.add_argument('--apply', action='store_true',
                            help='Write the rows. Without it, only report.')

    def handle(self, *args, as_username, event_code, usernames, reason, apply, **options):
        # Admin only, and active: the same people the Admin screen admits (role 'Admin',
        # T-1). System Admin and every other role are refused before anything is written.
        admin = (active_admins().select_related('user')
                 .filter(user__username=as_username).first())
        if admin is None:
            raise CommandError(f'{as_username!r} is not an active Admin; only an active '
                               f'Admin may add named recipients.')

        event = NOTIFICATION_EVENTS.get(event_code)
        if event is None:
            raise CommandError(f'There is no notification event {event_code!r}.')
        if not event.named_recipients:
            raise CommandError(f'{event_code!r} does not take named recipients.')
        if len((reason or '').strip()) < 10:
            raise CommandError('--reason must be at least 10 characters.')

        prefix = '' if apply else '[dry run] '
        counts = {'add': 0, 'active': 0, 'refused': 0}
        for username in dict.fromkeys(usernames):   # each username once, in given order
            profile = (UserProfile.objects.select_related('user')
                       .filter(user__username=username).first())
            if profile is None:
                counts['refused'] += 1
                self.stdout.write(f'{prefix}refuse  {username}: no such user')
                continue
            if active_named_recipient(event.code, profile) is not None:
                counts['active'] += 1
                self.stdout.write(f'{prefix}active  {username}: already a named recipient')
                continue
            if not (profile.is_active and profile.user.is_active):
                counts['refused'] += 1
                self.stdout.write(f'{prefix}refuse  {username}: deactivated')
                continue
            if not apply:
                counts['add'] += 1
                self.stdout.write(f'{prefix}add     {username}')
                continue
            try:
                row = add_named_recipient(event.code, profile, admin, reason)
            except NotificationAdminRefused as exc:
                counts['refused'] += 1
                self.stdout.write(f'refuse  {username}: {exc}')
                continue
            counts['add'] += 1
            self.stdout.write(f'add     {username} (#{row.pk})')

        verb = 'Added' if apply else 'Would add'
        self.stdout.write(f'{prefix}{verb} {counts["add"]}; already active {counts["active"]}; '
                          f'refused {counts["refused"]} — event {event.code}.')
