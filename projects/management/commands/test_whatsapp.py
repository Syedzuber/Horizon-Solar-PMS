"""
WhatsApp template test harness — as a Django management command.

WHERE TO PUT THIS FILE:
    projects/management/commands/test_whatsapp.py

    If those folders don't exist, create them and add empty __init__.py files:
        projects/management/__init__.py
        projects/management/commands/__init__.py

HOW TO RUN (plain, no piping):
    python manage.py test_whatsapp

WHAT IT DOES:
    Sends each template in TEMPLATES directly to the Interakt API (bypassing
    send_notification so we see the raw HTTP status + response body).
    Also calls send_notification so NotificationLog is still written.

EDIT TWO THINGS BELOW BEFORE RUNNING:
    1. TEST_USERNAME  — the UserProfile.user.username whose phone is YOURS.
    2. TEMPLATES      — the param values per template, in the Interakt-registered order.

KNOWN: this legacy mode ignores the WhatsApp master switch (it only prints its state)
and writes no NotificationLog row, whatever the text above says (SECONDARY_FINDINGS,
NTF-DCG S3a). Left as it was; the --event mode below does neither.

REGISTRY EVENT MODE (NTF-DCG S3a, ruling W7):
    python manage.py test_whatsapp --event <event_code> --user <username> [--dry-run]

    Sends ONE registry event's WhatsApp template, with the event's declared sample
    values, to that user's number (checked by the W5 rule). The event's Admin pause is
    ignored, because this test is what lets Admin switch it on; the master switch and
    the user's WhatsApp preference are not. Prints the exact header and body values,
    the payload and Interakt's raw response, and writes one NotificationLog row:
    channel 'whatsapp', event_code, template_name = the Interakt template, message
    starting '[TEST] '. A 'sent' row of the event's current template from the last 14
    days is what notify_admin.set_state() needs to switch the WhatsApp channel on.
    --dry-run prints the values and payload and sends and writes nothing.
"""

import json
import time
import requests as _requests

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from projects.models import UserProfile, NotificationLog, SystemSettings
from projects.notifications import (
    NO_VALID_WHATSAPP_NUMBER, _send_whatsapp, interakt_payload, send_notification,
    valid_whatsapp_number,
)
from projects.notify import absolute_url
from projects.notify_admin import WHATSAPP_TEST_MARKER
from projects.notify_registry import NOTIFICATION_EVENTS
from projects.whatsapp_params import describe_params, sanitise_params


# ----------------------------------------------------------------- EDIT THESE
TEST_USERNAME = "chetan"

TEMPLATES = {
    # header (1) + body (3) = 4 total
    "issue_created":        ["Miracle Hospital", "Subhash", "Miracle Hospital", "Chetan"],

    # header (1) + body (4) = 5 — CONFIRM body is 4, not 2
    "issue_resolved":       ["Miracle Hospital", "client not available", "Miracle Hospital","Pradeep", "abc.com"],

    # header (1) + body (4) = 5 total
    "assign_task":          ["Miracle Hospital", "Subhash", "Order Solar Panel", "Miracle Hospital", "abc.com"],

    # header (1) + body (3 or 4?) = CONFIRM — your label says 3, your list has 4
    "assign_project":       ["Miracle Hospital", "Miracle Hospital", "Design Engineer", "Chetan"],

    # header (1) + body (2) = 3 total
    "boq_acknowledged":     ["Miracle Hospital", "Miracle Hospital", "abc.com"],

    # header (1) + body (2) = 3 total
    "payment_notification": ["Miracle Hospital", "Milestone 1", "Miracle Hospital"],

    # header (1) + body (4) = 5 total: customer_name, boq_item, invoice_no, amount, vendor_name
    "invoice_paid":         ["Miracle Hospital", "Inverter", "INV-001", "50000", "HRP Suppliers"],
}
# ----------------------------------------------------------------- /EDIT THESE


class Command(BaseCommand):
    help = ("Send all legacy WhatsApp templates directly to Interakt and print raw HTTP "
            "responses; or, with --event and --user, send one registry event's WhatsApp "
            "template with its sample values to that user (the test Admin needs before "
            "switching the event's WhatsApp on).")

    def add_arguments(self, parser):
        parser.add_argument(
            '--event', default='', metavar='EVENT_CODE',
            help="A registry event that declares WhatsApp. Sends only that event's "
                 "template, with its declared sample values.")
        parser.add_argument(
            '--user', default='', metavar='USERNAME',
            help="With --event: the user whose profile number receives the test.")
        parser.add_argument(
            '--dry-run', action='store_true',
            help="With --event: print the values and payload; send and write nothing.")

    def handle(self, *args, **options):
        if options['event'] or options['user'] or options['dry_run']:
            return self.handle_event(options['event'], options['user'], options['dry_run'])
        return self.handle_legacy()

    # ------------------------------------------------------------------ W7
    def handle_event(self, event_code, username, dry_run):
        """One registry event's WhatsApp to one user (W7). Refusals are CommandErrors and
        write nothing; past them, the send writes exactly one NotificationLog row."""
        if not event_code or not username:
            raise CommandError('--event and --user are both required (and --dry-run '
                               'needs them too).')
        # The real registry only. The test-only event of tests_notify_whatsapp is never
        # in it (W8), so it cannot be sent from here.
        event = NOTIFICATION_EVENTS.get(event_code)
        if event is None:
            raise CommandError(f'No registry event {event_code!r}.')
        spec = event.whatsapp
        if spec is None:
            raise CommandError(f'Event {event_code!r} does not declare WhatsApp.')
        profile = (UserProfile.objects.select_related('user')
                   .filter(user__username=username).first())
        if profile is None:
            raise CommandError(f'No user {username!r}.')
        if not (profile.is_active and profile.user.is_active):
            raise CommandError(f'User {username!r} is deactivated.')
        digits = valid_whatsapp_number(profile.phone_number)
        if digits is None:
            raise CommandError(f'User {username!r}: {NO_VALID_WHATSAPP_NUMBER} '
                               f'(stored: {profile.phone_number!r}).')

        # The declared samples, with the link made absolute as notify() makes it for a
        # real send, so the test shows the URL a phone would really get.
        values = dict(event.samples())
        values['link'] = absolute_url(values.get('link', ''))
        raw = list(spec.params(values))
        if len(raw) != spec.param_count:
            raise CommandError(f'The params builder returned {len(raw)} values; '
                               f'param_count is {spec.param_count}.')
        params = sanitise_params(raw, header_fixed_chars=spec.header_fixed_chars,
                                 body_fixed_chars=spec.body_fixed_chars)

        settings_row = SystemSettings.objects.filter(pk=1).first()
        master_on = bool(settings_row and settings_row.whatsapp_enabled)
        api_key_set = bool(getattr(settings, 'INTERAKT_API_KEY', ''))

        out = self.stdout.write
        out(f'Event             : {event.code} ({event.label})')
        out(f'Interakt template : {spec.template}  language: {spec.language}')
        out(f'Recipient         : {username}  phone digits: {digits}')
        out(f'WhatsApp master   : {"ON" if master_on else "OFF"}')
        out(f'API key           : {"set" if api_key_set else "not set"}')
        out(f'Header value      : {params[0]!r}')
        for i, value in enumerate(params[1:], start=1):
            out(f'Body value {{{{{i}}}}}    : {value!r}')

        if dry_run:
            payload = interakt_payload(digits, spec.template, params,
                                       f'solarpms-{profile.pk}', spec.language)
            out(f'PAYLOAD  : {json.dumps(payload, ensure_ascii=False)}')
            out('DRY RUN: nothing sent, nothing logged.')
            return
        # The master switch is never bypassed (W7): refused before any row or request.
        if not master_on:
            raise CommandError('WhatsApp master switch is OFF; nothing sent.')
        if not profile.whatsapp_notifications:
            raise CommandError(f'User {username!r} has WhatsApp notifications off; '
                               f'nothing sent.')

        # _send_whatsapp() itself, so the test posts the bytes a real send of this event
        # would post and its row is logged by the same code. The base marks it a test.
        base = dict(recipient=profile, related_project=None, actor=None,
                    message=WHATSAPP_TEST_MARKER + describe_params(params),
                    template_name=spec.template, event_code=event.code)
        mark = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        result = _send_whatsapp(profile, spec.template, params, base,
                                language=spec.language, strict_phone=True)
        if result['payload'] is not None:
            out(f'PAYLOAD  : {json.dumps(result["payload"], ensure_ascii=False)}')
        out(f'HTTP     : {result["status_code"]}')
        out(f'RESPONSE : {result["response_text"]}')
        row = (NotificationLog.objects.filter(pk__gt=mark, recipient=profile,
                                              channel='whatsapp', event_code=event.code)
               .order_by('-pk').first())
        if row is not None:
            out(f'LOG ROW  : #{row.pk} status={row.status} '
                f'interakt_message_id={row.interakt_message_id!r} '
                f'error={row.error_detail!r}')

    # ------------------------------------------------------------------ legacy
    def handle_legacy(self):
        line = "=" * 72

        self.stdout.write("\n" + line)
        self.stdout.write("WhatsApp template test harness — RAW HTTP MODE")
        self.stdout.write(line)

        # --- pre-flight guards ---
        try:
            profile = UserProfile.objects.get(user__username=TEST_USERNAME)
        except UserProfile.DoesNotExist:
            self.stdout.write(f"\n[STOP] No UserProfile with username '{TEST_USERNAME}'.")
            return

        if not profile.phone_number:
            self.stdout.write(f"\n[STOP] '{TEST_USERNAME}' has no phone_number.")
            return

        api_key = getattr(settings, 'INTERAKT_API_KEY', '')
        if not api_key:
            self.stdout.write("\n[STOP] INTERAKT_API_KEY not set in settings.")
            return

        # Normalise phone — same logic as _send_whatsapp in notifications.py
        phone = profile.phone_number.strip()
        if phone.startswith('+91'):
            phone_digits = phone[3:]
        elif phone.startswith('91') and len(phone) == 12:
            phone_digits = phone[2:]
        else:
            phone_digits = phone

        settings_row = SystemSettings.objects.filter(pk=1).first()
        master_on = bool(settings_row and settings_row.whatsapp_enabled)

        self.stdout.write(f"\nRecipient profile : {TEST_USERNAME}")
        self.stdout.write(f"Phone on file     : {profile.phone_number}  ->  digits: {phone_digits}")
        self.stdout.write(f"WhatsApp master   : {'ON' if master_on else 'OFF'}")
        # W10: never print any part of the key.
        self.stdout.write(f"API key           : {'set' if api_key else 'not set'}")

        # --- fire each template directly at Interakt ---
        for template, param_list in TEMPLATES.items():
            params = [str(p) for p in param_list]
            payload = {
                'countryCode': '+91',
                'phoneNumber': phone_digits,
                'callbackData': 'solarpms-rawtest',
                'type': 'Template',
                'template': {
                    'name': template,
                    'languageCode': 'en',
                    'headerValues': params[:1],
                    'bodyValues':   params[1:],
                },
            }

            self.stdout.write("\n" + "-" * 72)
            self.stdout.write(f"TEMPLATE : {template}")
            self.stdout.write(f"PAYLOAD  : {json.dumps(payload)}")

            try:
                resp = _requests.post(
                    'https://api.interakt.ai/v1/public/message/',
                    headers={
                        'Authorization': f'Basic {api_key}',
                        'Content-Type': 'application/json',
                    },
                    json=payload,
                    timeout=10,
                )
                self.stdout.write(f"HTTP     : {resp.status_code}")
                self.stdout.write(f"RESPONSE : {resp.text}")
            except Exception as e:
                self.stdout.write(f"EXCEPTION: {e!r}")

            time.sleep(2)

        self.stdout.write("\n" + line)
        self.stdout.write("RAW OUTPUT COMPLETE — check above for HTTP status and response body")
        self.stdout.write(line + "\n")
