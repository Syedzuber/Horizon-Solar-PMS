"""
WhatsApp template test harness — SolarPMS
Runs all six approved Interakt templates through send_notification() to YOUR phone,
then prints a single status board: SENT / FAILED / SKIPPED + error detail per template.

HOW TO RUN:
    python manage.py shell < test_whatsapp_templates.py

BEFORE RUNNING — three things or every row will mislead you:
  1. Set TEST_USERNAME below to a UserProfile whose phone_number is YOUR phone
     (you must be able to see the messages arrive).
  2. SystemSettings.whatsapp_enabled must be True, or every row prints SKIPPED.
     (This script checks and warns you.)
  3. The TEMPLATE_PARAMS below must match each template's REGISTERED order in the
     Interakt console (header + body vars, positional). Fix these first — that's
     the whole point of the exercise. Placeholders are marked CONFIRM_IN_CONSOLE.

WHAT 'SENT' DOES AND DOESN'T PROVE:
  SENT = Interakt accepted the request. It does NOT prove the variables landed in
  the right slots. You MUST look at each message on your phone and confirm the
  content reads correctly (right name in the name slot, etc.). A scrambled-but-
  accepted message logs SENT. Your eyes are the second test.
"""

import time
from django.utils import timezone
from projects.models import UserProfile, NotificationLog, SystemSettings
from projects.notifications import send_notification

# ------------------------------------------------------------------ CONFIG
TEST_USERNAME = "chetan"   # <-- UserProfile.user.username with YOUR phone number

# Each entry: template api-name -> ordered list of param values.
# ORDER AND COUNT must match the Interakt console registration for that template
# (header variables count too, and come in their registered position).
# Replace every "CONFIRM_IN_CONSOLE" after checking the console.
TEMPLATES = {
    "issue_created":        ["Miracle Hospital","Subhash", "Miracle Hospital", "Chetan"],
    "issue_resolved":       ["Miracle Hospital", "client not available", "Pradeep", ""],
    "assign_task":          ["Miracle Hospital", "Subhash", "Order Soalr Panel", "Miracle Hospital", "abc.com"],
    "assign_project":       ["Miracle Hospital", "Miracle Hospital", "Design Engineer","Chetan"],
    "boq_acknowledged":     ["Miracle Hospital", "Miracle Hospital","abc.com"],
    "payment_notification": ["Miracle Hospital", "Milestone 1", "Miracle Hospital"],
    "invoice_paid": ["Miracle Hospital", "Inverter", "HRP"],
}
# ------------------------------------------------------------------ /CONFIG


def banner(text):
    print("\n" + "=" * 64)
    print(text)
    print("=" * 64)


banner("WhatsApp template test harness")

# --- Pre-flight guards -------------------------------------------------
errors = []

try:
    profile = UserProfile.objects.get(user__username=TEST_USERNAME)
except UserProfile.DoesNotExist:
    print(f"\n[STOP] No UserProfile with username '{TEST_USERNAME}'.")
    print("       Set TEST_USERNAME to a real profile with YOUR phone number.")
    raise SystemExit

if TEST_USERNAME == "CHANGE_ME":
    print("\n[STOP] TEST_USERNAME is still 'CHANGE_ME'. Edit the script first.")
    raise SystemExit

if not profile.phone_number:
    print(f"\n[STOP] '{TEST_USERNAME}' has no phone_number. WhatsApp can't send.")
    raise SystemExit

settings_row = SystemSettings.objects.filter(pk=1).first()
master_on = bool(settings_row and settings_row.whatsapp_enabled)

print(f"\nRecipient profile : {TEST_USERNAME}")
print(f"Phone on file     : {profile.phone_number}")
print(f"WhatsApp master   : {'ON' if master_on else 'OFF  <-- every row will SKIP'}")

# Warn (don't stop) about unconfirmed params — the test still runs so you can
# see which ones Interakt rejects, but flag them loudly.
unconfirmed = [t for t, p in TEMPLATES.items() if any(v == "CONFIRM_IN_CONSOLE" for v in p)]
if unconfirmed:
    print("\n[WARN] These templates still have CONFIRM_IN_CONSOLE placeholders:")
    for t in unconfirmed:
        print(f"         - {t}")
    print("       They will almost certainly FAIL (400). Fix the param lists,")
    print("       then re-run for a clean board.")

# --- Run each template -------------------------------------------------
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
        # The chokepoint should swallow send errors itself; this catch is only
        # for unexpected programming errors so the loop still finishes.
        print(f"  [unexpected] {template}: {e!r}")

    # Read back the log row this call just produced.
    log = (NotificationLog.objects
           .filter(channel="whatsapp", created_at__gte=start)
           .order_by("-created_at")
           .first())

    if log:
        results.append((template, log.status, (log.error_detail or "").strip()))
    else:
        results.append((template, "NO LOG", "no NotificationLog row was written"))

    time.sleep(1)  # be gentle on the Interakt API between sends

# --- Status board ------------------------------------------------------
banner("RESULTS — status board")

print(f"{'TEMPLATE':<24} {'STATUS':<8} DETAIL")
print("-" * 64)
for template, status, detail in results:
    short = detail if len(detail) <= 80 else detail[:77] + "..."
    print(f"{template:<24} {status:<8} {short}")

sent    = sum(1 for _, s, _ in results if s == "sent")
failed  = sum(1 for _, s, _ in results if s == "failed")
skipped = sum(1 for _, s, _ in results if s == "skipped")
other   = len(results) - sent - failed - skipped

print("-" * 64)
print(f"sent={sent}  failed={failed}  skipped={skipped}  other={other}")

banner("NOW CHECK YOUR PHONE")
print("Every 'sent' row above only means Interakt ACCEPTED the request.")
print("Open WhatsApp and confirm each message arrived AND the variables are in")
print("the right slots (correct name/project/etc). A scrambled message can still")
print("log 'sent'. Your eyes are the second test.\n")
