"""
External notification chokepoint for SolarPMS.

Every outbound notification — in-app, WhatsApp (Interakt), email (ZeptoMail) —
flows through send_notification(). This ensures:
  - Master switch and per-user preferences are always checked.
  - Every attempt (sent / failed / skipped) is logged in NotificationLog.
  - A failed send NEVER propagates — the triggering action always commits.

Layer 5 guardrails:
  - Phone numbers stored as 10-digit; +91 country code added here.
  - recipient must always be a UserProfile, never a raw User.
  - whatsapp_enabled / email_enabled default False — flip ON only after live test.
"""

import base64
import logging
import re

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# The key under which send_notification() carries log_row_id inside its `base` dict to
# _log(). Never a NotificationLog field, so _log() removes it before any create().
_PENDING_ROW = '_pending_row'

# How much error text a deferred row keeps (S5). A provider can return a long body or
# a traceback-sized message; the stuck/failed listings need the start of it, not all.
PENDING_ERROR_LIMIT = 1000


def send_notification(
    recipient,
    message,
    channels=None,
    link='',
    subject='',
    template=None,
    template_params=None,
    related_project=None,
    actor=None,
    html_message=None,
    event_code=None,
    template_version=None,
    log_row_id=None,
    whatsapp_language='en',
    whatsapp_strict_phone=False,
    attachments=None,
):
    """
    Send a notification to recipient on one or more channels.

    Args:
        recipient       : UserProfile — the person to notify.
        message         : str — in-app / email body text. For email, this is the
                          plain-text fallback (textbody) when html_message is given.
        channels        : list of 'in_app' | 'whatsapp' | 'email'. Defaults to ['in_app'].
        link            : relative URL for in-app click-through (e.g. '/issues/5/').
        subject         : email subject line.
        html_message    : optional str — HTML email body (htmlbody). When provided,
                          `message` is still sent as the plain-text alternative.
        template        : Interakt template API name (required if 'whatsapp' in channels).
        template_params : list — positional values in Interakt's REGISTERED order.
                          Pull the exact order from the Interakt console, NOT the preview.
                          Header and body variables are counted separately.
        related_project : Project FK stored on the log row.
        actor           : UserProfile who triggered the send (stored on log row).
        event_code      : notify_registry event code, stored on every log row. Passed
                          only by notify.notify(); None for every other caller.
        template_version: NotificationTemplate the email was rendered from, stored on
                          every log row. Passed only by notify.notify().
        log_row_id      : pk of a 'pending' NotificationLog row to FINISH instead of
                          creating a new one (NTF-DCG S2). Passed only by notify_deferred,
                          with one channel. None for every other caller, whose rows are
                          created exactly as before.
        whatsapp_language     : Meta language code of the Interakt template. 'en', the
                          value every legacy caller has always sent, unless a registry
                          event's WhatsAppSpec names another (NTF-DCG S3a, W1).
        whatsapp_strict_phone : True refuses a number that is not a valid Indian mobile
                          after normalising (ruling W5). Passed only by notify()'s
                          WhatsApp leg; legacy callers keep the old, looser check.
        attachments     : email only. A list of {'name', 'mime_type', 'content' (bytes)}
                          files to attach (SNAP-1: the daily snapshot PNGs). None, the
                          default every other caller keeps, sends exactly the payload it
                          always did, with no 'attachments' key.
    """
    from .models import Notification, NotificationLog, SystemSettings

    if channels is None:
        channels = ['in_app']

    try:
        sys = SystemSettings.get()
        wa_enabled    = sys.whatsapp_enabled
        email_enabled = sys.email_enabled
    except Exception as e:
        logger.error('send_notification: could not read SystemSettings — %s', e)
        wa_enabled    = False
        email_enabled = False

    _base = dict(
        recipient=recipient,
        related_project=related_project,
        actor=actor,
        message=message,
        template_name=template or '',
    )
    # Added to the row only when given, so a caller that passes neither creates its log
    # rows with exactly the keyword arguments it always did (T1).
    if event_code is not None:
        _base['event_code'] = event_code
    if template_version is not None:
        _base['template_version'] = template_version
    # Same rule: only a deferred send carries it, so every other base is unchanged.
    if log_row_id is not None:
        _base[_PENDING_ROW] = log_row_id

    for channel in channels:
        if channel == 'in_app':
            _send_in_app(recipient, message, link, _base)

        elif channel == 'whatsapp':
            if not wa_enabled:
                _log(_base, 'whatsapp', 'skipped', 'Master switch off')
                continue
            if not recipient.whatsapp_notifications:
                _log(_base, 'whatsapp', 'skipped', 'User preference off')
                continue
            _send_whatsapp(recipient, template, template_params, _base,
                           language=whatsapp_language, strict_phone=whatsapp_strict_phone)

        elif channel == 'email':
            if not email_enabled:
                _log(_base, 'email', 'skipped', 'Master switch off')
                continue
            if not recipient.email_notifications:
                _log(_base, 'email', 'skipped', 'User preference off')
                continue
            _send_email(recipient, subject, message, _base, html_message,
                        attachments=attachments)


# ---------------------------------------------------------------------------
# Internal helpers — not exported
# ---------------------------------------------------------------------------

def _send_in_app(recipient, message, link, base):
    from .models import Notification
    try:
        Notification.objects.create(recipient=recipient, message=message, link=link)
        _log(base, 'in_app', 'sent')
    except Exception as e:
        logger.error('send_notification: in_app failed for %s — %s', recipient, e)
        _log(base, 'in_app', 'failed', str(e))


INTERAKT_URL = 'https://api.interakt.ai/v1/public/message/'

# A WhatsApp number the strict check accepts (ruling W5): ten digits starting 6-9, the
# rule every user form already applies to UserProfile.phone_number.
_INDIAN_MOBILE_RE = re.compile(r'[6-9][0-9]{9}')

NO_VALID_WHATSAPP_NUMBER = 'no valid WhatsApp number on profile'


def whatsapp_number(raw):
    """`raw` as Interakt's phoneNumber: stripped, with a +91 / 12-digit 91 prefix removed.

    The one normaliser, used by _send_whatsapp() as it always was, by notify()'s
    duplicate-phone check (W9) and by test_whatsapp. Nothing is validated here: a legacy
    send has always posted whatever this returns.
    """
    # DB stores raw 10-digit number without country code. Guard against
    # double-prefixing if someone saves "+91..." or "91..." in the DB.
    phone = (raw or '').strip()
    if phone.startswith('+91'):
        return phone[3:]
    if phone.startswith('91') and len(phone) == 12:
        return phone[2:]
    return phone


def valid_whatsapp_number(raw):
    """whatsapp_number(raw) when it is a valid Indian mobile, else None (ruling W5)."""
    digits = whatsapp_number(raw)
    return digits if _INDIAN_MOBILE_RE.fullmatch(digits) else None


def interakt_payload(phone_digits, template, template_params, callback, language='en'):
    """The Interakt template-message body. The single home of its shape.

    Interakt template API: header and body variables are separate fields.
    Convention: pass template_params as one flat list — first element is the single
    header variable value, remaining elements are body variable values (in registered
    order). This matches all our approved templates which each have exactly 1 header
    variable. Built exactly as _send_whatsapp() built it before NTF-DCG S3a, key order
    included, so a legacy send posts the same bytes; `language` defaults to the 'en'
    that was hard-coded.
    """
    params = [str(p) for p in (template_params or [])]
    return {
        'countryCode': '+91',
        'phoneNumber': phone_digits,
        'callbackData': callback,
        'type': 'Template',
        'template': {
            'name': template,
            'languageCode': language,
            'headerValues': params[:1],   # first element = the single header variable
            'bodyValues':   params[1:],   # remaining elements = body variables
        },
    }


def _send_whatsapp(recipient, template, template_params, base, language='en',
                   strict_phone=False):
    """Post one template message and log its outcome. Never raises.

    Returns {'payload', 'status_code', 'response_text'} for the caller that wants to
    print what happened (test_whatsapp, W7); every key is None when no request was made.
    send_notification() ignores it.
    """
    result = {'payload': None, 'status_code': None, 'response_text': None}
    if not template:
        _log(base, 'whatsapp', 'failed', 'No template name provided')
        return result

    if strict_phone:
        phone_digits = valid_whatsapp_number(recipient.phone_number)
        if phone_digits is None:
            _log(base, 'whatsapp', 'failed', NO_VALID_WHATSAPP_NUMBER)
            return result
    else:
        phone = (recipient.phone_number or '').strip()
        if not phone:
            _log(base, 'whatsapp', 'failed', 'Recipient has no phone number')
            return result
        phone_digits = whatsapp_number(phone)

    api_key = getattr(settings, 'INTERAKT_API_KEY', '')
    if not api_key:
        _log(base, 'whatsapp', 'failed', 'INTERAKT_API_KEY not configured')
        return result

    payload = interakt_payload(phone_digits, template, template_params,
                               f'solarpms-{recipient.pk}', language)
    result['payload'] = payload

    try:
        resp = requests.post(
            INTERAKT_URL,
            headers={
                'Authorization': f'Basic {api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=10,
        )
        result['status_code'], result['response_text'] = resp.status_code, resp.text
        body = resp.text[:500]
        if 200 <= resp.status_code < 300:
            try:
                resp_data = resp.json()
                msg_id = resp_data.get('id', '')
            except Exception:
                msg_id = ''
            _log(base, 'whatsapp', 'sent', interakt_message_id=msg_id)
            return result
        _log(base, 'whatsapp', 'failed', f'HTTP {resp.status_code}: {body}')
    except Exception as e:
        _log(base, 'whatsapp', 'failed', str(e))
    return result


def _zeptomail_post(to_email, to_name, subject, text_body, html_body=None,
                    attachments=None):
    """Low-level ZeptoMail HTTP send. Returns (ok: bool, detail: str).

    `attachments` (SNAP-1) is a list of {'name', 'mime_type', 'content' (bytes)}; each is
    base64-encoded into ZeptoMail's `attachments` array. Without it the payload has no
    'attachments' key, byte-identical to before (tests_dashboard_snapshot pins it).

    Single home for the ZeptoMail endpoint + payload shape, shared by _send_email
    (per-user notifications) and send_aggregate_email (company-wide digest) so neither
    duplicates the API contract. htmlbody is included only when html_body is given, so a
    text-only caller produces a byte-identical payload to before this was extracted.
    """
    api_key    = getattr(settings, 'ZEPTOMAIL_API_KEY', '')
    from_email = getattr(settings, 'ZEPTOMAIL_FROM_EMAIL', '')
    if not api_key or not from_email:
        return False, 'ZEPTOMAIL_API_KEY or ZEPTOMAIL_FROM_EMAIL not configured'

    payload = {
        'from':    {'address': from_email, 'name': 'Horizon Solar'},
        'to':      [{'email_address': {'address': to_email, 'name': to_name}}],
        'subject': subject or 'Horizon Solar — Notification',
        'textbody': text_body,
    }
    # Reply-to on every email this sends, per-user and digest alike (T3a, P4): From is
    # a no-reply mailbox, so a reply would otherwise go nowhere. Read at call time so
    # override_settings reaches it. send_raw_email builds its own payload and is
    # deliberately left without one.
    reply_to = getattr(settings, 'NOTIFY_REPLY_TO', '')
    if reply_to:
        payload['reply_to'] = [{'address': reply_to, 'name': 'Horizon Solar PMS'}]
    # ZeptoMail accepts htmlbody + textbody together (multipart alternative). Clients
    # that can render HTML use htmlbody; the rest fall back to the plain-text message.
    if html_body:
        payload['htmlbody'] = html_body
    if attachments:
        payload['attachments'] = [
            {'name': item['name'], 'mime_type': item['mime_type'],
             'content': base64.b64encode(item['content']).decode('ascii')}
            for item in attachments
        ]

    try:
        resp = requests.post(
            'https://api.zeptomail.in/v1.1/email',
            headers={
                'Authorization': api_key,  # stored as full "Zoho-enczapikey <token>" value
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return True, ''
        return False, f'HTTP {resp.status_code}: {resp.text[:500]}'
    except Exception as e:
        return False, str(e)


def _send_email(recipient, subject, message, base, html_message=None, attachments=None):
    email = (recipient.user.email or '').strip()
    if not email:
        _log(base, 'email', 'failed', 'Recipient has no email address')
        return

    display_name = recipient.user.get_full_name() or recipient.user.username
    # Attachments are passed only when there are some, so every existing caller makes
    # exactly the call it always made (and the tests that patch _zeptomail_post with a
    # five-argument stand-in keep working).
    if attachments:
        ok, detail = _zeptomail_post(email, display_name, subject, message, html_message,
                                     attachments=attachments)
    else:
        ok, detail = _zeptomail_post(email, display_name, subject, message, html_message)
    _log(base, 'email', 'sent' if ok else 'failed', '' if ok else detail)

    # --- SendGrid fallback (commented out; uncomment if ZeptoMail domain verification stalls) ---
    # from sendgrid import SendGridAPIClient
    # from sendgrid.helpers.mail import Mail
    # sg_key = getattr(settings, 'SENDGRID_API_KEY', '')
    # if not sg_key:
    #     _log(base, 'email', 'failed', 'SENDGRID_API_KEY not configured')
    #     return
    # mail = Mail(
    #     from_email='noreply@horizonrenewablepower.com',
    #     to_emails=email,
    #     subject=subject or 'Horizon Solar — Notification',
    #     plain_text_content=message,
    # )
    # resp = SendGridAPIClient(sg_key).send(mail)
    # if resp.status_code in (200, 202):
    #     _log(base, 'email', 'sent')
    # else:
    #     _log(base, 'email', 'failed', f'HTTP {resp.status_code}: {resp.body}')


def send_raw_email(to_email, subject, body):
    """
    Send a transactional email to any address without requiring a UserProfile.
    Used for fixed system-level alerts (e.g. unassigned project from Zoho webhook).
    Does NOT check master switch — this is a platform-level alert, not user notification.
    """
    api_key    = getattr(settings, 'ZEPTOMAIL_API_KEY', '')
    from_email = getattr(settings, 'ZEPTOMAIL_FROM_EMAIL', '')
    if not api_key or not from_email:
        logger.error('send_raw_email: ZEPTOMAIL_API_KEY or ZEPTOMAIL_FROM_EMAIL not configured')
        return
    try:
        resp = requests.post(
            'https://api.zeptomail.in/v1.1/email',
            headers={'Authorization': api_key, 'Content-Type': 'application/json'},
            json={
                'from': {'address': from_email, 'name': 'Horizon Solar'},
                'to':   [{'email_address': {'address': to_email, 'name': 'Admin'}}],
                'subject': subject,
                'textbody': body,
            },
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            logger.error('send_raw_email: HTTP %s — %s', resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.error('send_raw_email: %s', exc)


def send_aggregate_email(to_email, subject, text_body, html_body=None,
                         log_recipient=None, template_name=''):
    """
    Send a company-wide digest email to a FIXED address (Admin/HR), not a UserProfile.

    Gated ONLY by the global master switch (SystemSettings.email_enabled) — there is no
    per-recipient preference row for these addresses. Reuses _zeptomail_post so the HTML
    body and API contract match the per-user path.

    Logging: NotificationLog.recipient is a required FK to UserProfile, so a row is written
    only when log_recipient (a UserProfile whose email the caller matched to this address)
    is supplied. If the address has no matching user account, the send still happens but is
    recorded to the application log instead — it cannot appear in NotificationLog.
    """
    from .models import SystemSettings

    to_email = (to_email or '').strip()
    base = None
    if log_recipient is not None:
        base = dict(recipient=log_recipient, related_project=None, actor=None,
                    message=text_body, template_name=template_name)

    def _record(status, detail=''):
        if base is not None:
            _log(base, 'email', status, detail)
        else:
            log_fn = logger.info if status == 'sent' else logger.warning
            log_fn('send_aggregate_email: %s -> %s (%s) %s', status, to_email, template_name, detail)

    if not to_email:
        _record('failed', 'No recipient address')
        return

    try:
        email_enabled = SystemSettings.get().email_enabled
    except Exception as e:
        logger.error('send_aggregate_email: could not read SystemSettings — %s', e)
        email_enabled = False
    if not email_enabled:
        _record('skipped', 'Master switch off')
        return

    ok, detail = _zeptomail_post(to_email, 'Horizon Solar', subject, text_body, html_body)
    _record('sent' if ok else 'failed', '' if ok else detail)


def finish_pending(row_id, status, error_detail='', interakt_message_id=''):
    """Move the 'pending' row `row_id` to `status`, stamping finished_at. Never raises.

    The one writer of a deferred row's outcome: _log() calls it for a send made with
    log_row_id, and notify_deferred calls it when the job itself fails. Error text is
    cut to PENDING_ERROR_LIMIT.
    """
    # Late import, as in every helper of this module: notifications.py never imports
    # models at load time. No cycle is known today; kept for consistency.
    from .models import NotificationLog
    try:
        # RACE: filter(status='pending').update() and not get() + save(). Only the one
        # job that owns the row should finish it, and a row already finished (by that
        # job, or by a person in a shell) must not be overwritten by a late second
        # writer. The status condition makes the update a no-op for anyone but the first.
        NotificationLog.objects.filter(pk=row_id, status='pending').update(
            status=status,
            error_detail=(error_detail or '')[:PENDING_ERROR_LIMIT],
            interakt_message_id=interakt_message_id or '',
            finished_at=timezone.now(),
        )
    except Exception as e:
        logger.error('finish_pending: NotificationLog %s update failed — %s', row_id, e)


def _log(base, channel, status, error_detail='', interakt_message_id='',
         event_code=None, template_version=None):
    # A deferred send finishes its own pending row instead of adding a second one (S5).
    # Absent from every other base, so their rows are created exactly as before.
    if _PENDING_ROW in base:
        finish_pending(base[_PENDING_ROW], status, error_detail, interakt_message_id)
        return
    from .models import NotificationLog
    # event_code / template_version are for the rows notify.notify() writes itself
    # (a paused event, a failed render), whose base it builds without them. Rows from
    # send_notification() carry them in `base` instead; no caller passes both.
    extra = {}
    if event_code is not None:
        extra['event_code'] = event_code
    if template_version is not None:
        extra['template_version'] = template_version
    try:
        NotificationLog.objects.create(
            **base,
            channel=channel,
            status=status,
            error_detail=error_detail,
            interakt_message_id=interakt_message_id,
            **extra,
        )
    except Exception as e:
        logger.error('_log: NotificationLog.create failed — %s', e)
