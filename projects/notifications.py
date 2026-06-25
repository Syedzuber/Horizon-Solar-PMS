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

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


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
):
    """
    Send a notification to recipient on one or more channels.

    Args:
        recipient       : UserProfile — the person to notify.
        message         : str — in-app / email body text.
        channels        : list of 'in_app' | 'whatsapp' | 'email'. Defaults to ['in_app'].
        link            : relative URL for in-app click-through (e.g. '/issues/5/').
        subject         : email subject line.
        template        : Interakt template API name (required if 'whatsapp' in channels).
        template_params : list — positional values in Interakt's REGISTERED order.
                          Pull the exact order from the Interakt console, NOT the preview.
                          Header and body variables are counted separately.
        related_project : Project FK stored on the log row.
        actor           : UserProfile who triggered the send (stored on log row).
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
            _send_whatsapp(recipient, template, template_params, _base)

        elif channel == 'email':
            if not email_enabled:
                _log(_base, 'email', 'skipped', 'Master switch off')
                continue
            if not recipient.email_notifications:
                _log(_base, 'email', 'skipped', 'User preference off')
                continue
            _send_email(recipient, subject, message, _base)


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


def _send_whatsapp(recipient, template, template_params, base):
    if not template:
        _log(base, 'whatsapp', 'failed', 'No template name provided')
        return

    phone = (recipient.phone_number or '').strip()
    if not phone:
        _log(base, 'whatsapp', 'failed', 'Recipient has no phone number')
        return

    # Normalise: DB stores raw 10-digit number without country code.
    # Guard against double-prefixing if someone saves "+91..." or "91..." in the DB.
    if phone.startswith('+91'):
        phone_digits = phone[3:]
    elif phone.startswith('91') and len(phone) == 12:
        phone_digits = phone[2:]
    else:
        phone_digits = phone

    api_key = getattr(settings, 'INTERAKT_API_KEY', '')
    if not api_key:
        _log(base, 'whatsapp', 'failed', 'INTERAKT_API_KEY not configured')
        return

    # Interakt template API: header and body variables are separate fields.
    # Convention: pass template_params as one flat list — first element is the single
    # header variable value, remaining elements are body variable values (in registered order).
    # This matches all our approved templates which each have exactly 1 header variable.
    params = [str(p) for p in (template_params or [])]
    payload = {
        'countryCode': '+91',
        'phoneNumber': phone_digits,
        'callbackData': f'solarpms-{recipient.pk}',
        'type': 'Template',
        'template': {
            'name': template,
            'languageCode': 'en',
            'headerValues': params[:1],   # first element = the single header variable
            'bodyValues':   params[1:],   # remaining elements = body variables
        },
    }

    try:
        resp = requests.post(
            'https://api.interakt.ai/v1/public/message/',
            headers={
                'Authorization': f'Basic {api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=10,
        )
        body = resp.text[:500]
        if 200 <= resp.status_code < 300:
            try:
                resp_data = resp.json()
                msg_id = resp_data.get('id', '')
            except Exception:
                msg_id = ''
            _log(base, 'whatsapp', 'sent', interakt_message_id=msg_id)
            return
        _log(base, 'whatsapp', 'failed', f'HTTP {resp.status_code}: {body}')
    except Exception as e:
        _log(base, 'whatsapp', 'failed', str(e))


def _send_email(recipient, subject, message, base):
    email = (recipient.user.email or '').strip()
    if not email:
        _log(base, 'email', 'failed', 'Recipient has no email address')
        return

    api_key    = getattr(settings, 'ZEPTOMAIL_API_KEY', '')
    from_email = getattr(settings, 'ZEPTOMAIL_FROM_EMAIL', '')

    if not api_key or not from_email:
        _log(base, 'email', 'failed', 'ZEPTOMAIL_API_KEY or ZEPTOMAIL_FROM_EMAIL not configured')
        return

    display_name = recipient.user.get_full_name() or recipient.user.username
    payload = {
        'from':    {'address': from_email, 'name': 'Horizon Solar'},
        'to':      [{'email_address': {'address': email, 'name': display_name}}],
        'subject': subject or 'Horizon Solar — Notification',
        'textbody': message,
    }

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
            _log(base, 'email', 'sent')
        else:
            _log(base, 'email', 'failed', f'HTTP {resp.status_code}: {resp.text[:500]}')
    except Exception as e:
        _log(base, 'email', 'failed', str(e))

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


def _log(base, channel, status, error_detail='', interakt_message_id=''):
    from .models import NotificationLog
    try:
        NotificationLog.objects.create(
            **base,
            channel=channel,
            status=status,
            error_detail=error_detail,
            interakt_message_id=interakt_message_id,
        )
    except Exception as e:
        logger.error('_log: NotificationLog.create failed — %s', e)
