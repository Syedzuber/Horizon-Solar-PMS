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
    Sends each template in TEMPLATES to the recipient's phone via send_notification(),
    reads back each NotificationLog row, prints a status board.

EDIT TWO THINGS BELOW BEFORE RUNNING:
    1. TEST_USERNAME  — the UserProfile.user.username whose phone is YOURS.
    2. TEMPLATES      — the param values per template, in the Interakt-registered order.
"""

import time
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

    # header (1) + body (2) = 3 total
    "invoice_paid":         ["Miracle Hospital", "Inverter", "HRP"],
}
# ----------------------------------------------------------------- /EDIT THESE


class Command(BaseCommand):
    help = "Send all WhatsApp templates to a test phone and print a status board."

    def handle(self, *args, **options):
        line = "=" * 64

        self.stdout.write("\n" + line)
        self.stdout.write("WhatsApp template test harness")
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

        settings_row = SystemSettings.objects.filter(pk=1).first()
        master_on = bool(settings_row and settings_row.whatsapp_enabled)

        self.stdout.write(f"\nRecipient profile : {TEST_USERNAME}")
        self.stdout.write(f"Phone on file     : {profile.phone_number}")
        self.stdout.write(f"WhatsApp master   : {'ON' if master_on else 'OFF  <-- every row will SKIP'}")

        # --- run each template ---
        results = []
        for template, params in TEMPLATES.items():
            start = timezone.now()
            try:
                send_notification(
                    recipient=profile,
                    message=f"[TEST] {template}",
                    channels=["whatsapp"],
                    template=template,
                    template_params=params,
                )
            except Exception as e:
                self.stdout.write(f"  [unexpected] {template}: {e!r}")

            log = (NotificationLog.objects
                   .filter(channel="whatsapp", created_at__gte=start)
                   .order_by("-created_at")
                   .first())

            if log:
                results.append((template, log.status, (log.error_detail or "").strip()))
            else:
                results.append((template, "NO LOG", "no NotificationLog row written"))

            time.sleep(1)

        # --- status board ---
        self.stdout.write("\n" + line)
        self.stdout.write("RESULTS — status board")
        self.stdout.write(line)
        self.stdout.write(f"{'TEMPLATE':<24} {'STATUS':<8} DETAIL")
        self.stdout.write("-" * 64)
        for template, status, detail in results:
            short = detail if len(detail) <= 80 else detail[:77] + "..."
            self.stdout.write(f"{template:<24} {status:<8} {short}")

        sent    = sum(1 for _, s, _ in results if s == "sent")
        failed  = sum(1 for _, s, _ in results if s == "failed")
        skipped = sum(1 for _, s, _ in results if s == "skipped")
        other   = len(results) - sent - failed - skipped
        self.stdout.write("-" * 64)
        self.stdout.write(f"sent={sent}  failed={failed}  skipped={skipped}  other={other}")

        self.stdout.write("\n" + line)
        self.stdout.write("NOW CHECK YOUR PHONE")
        self.stdout.write(line)
        self.stdout.write("A 'sent' row only means Interakt ACCEPTED the request.")
        self.stdout.write("Open WhatsApp and confirm each message arrived AND the variables")
        self.stdout.write("are in the right slots. A scrambled message still logs 'sent'.\n")