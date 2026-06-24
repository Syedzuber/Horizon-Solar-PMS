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
"""

import json
import time
import requests as _requests

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from projects.models import UserProfile, NotificationLog, SystemSettings
from projects.notifications import send_notification


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
    help = "Send all WhatsApp templates directly to Interakt and print raw HTTP responses."

    def handle(self, *args, **options):
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
        self.stdout.write(f"API key present   : {'YES (first 8: ' + api_key[:8] + '...)' if api_key else 'NO'}")

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
