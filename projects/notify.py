"""
notify() — send one registered event to a set of people (T1, spec v0.1).

The layer above send_notification(). A call site names an event from
notify_registry, the people to tell and the event's values; notify() decides what
each person receives:

  * in-app, always, from the event's code-owned in_app_text;
  * email, unless Admin has paused the event's email channel, worded by the event's
    ACTIVE NotificationTemplate or, when there is none, by the code default, and
    wrapped in projects/email/base_email.html / .txt.

Every attempt still goes through send_notification(), so the master switch, each
person's email preference and NotificationLog all apply exactly as before; the rows
notify() causes carry event_code and, for an email rendered from a stored version,
template_version. The two kinds of row notify() writes itself — 'skipped' for a paused
event, 'failed' for anything that went wrong for one person — go through the same _log().

WHATSAPP (NTF-DCG S3a). An event whose registry entry declares a WhatsAppSpec also gets
a WhatsApp leg, unless Admin has left it paused, which is its state until switched on
(default_state_for). Such an event is always deferred (a registry rule), so the send
goes through notify_deferred like a deferred email: one pending row per recipient,
finished by send_notification(). The values are cleaned and cut by
whatsapp_params.sanitise_params() first, and within one send a phone number gets one
message however many recipients share it (W9).

DEFERRED EVENTS (NTF-DCG S2). For an event registered with deferred=True, the email is
rendered here as usual and then handed to notify_deferred, which writes a 'pending' row
and makes the send_notification() call from a background thread. In-app, recipients and
rendering stay on this thread. Every other event is untouched.

RENDERING NEVER USES THE TEMPLATE ENGINE ON STORED TEXT. Subject and body are Admin-typed;
running them through Template(), format() or format_map() would let `{% load %}`,
`{{ request }}` or `{0.__class__}` reach code. Placeholders are `{{ name }}` only, found by
regex and replaced by a function; anything else between braces is a RenderError. The
layout files ARE Django templates, but they are code, and the rendered body reaches them
as a context value, which the engine never parses.

NEVER RAISES TO ITS CALLER after the event code is checked: the action that fired the
event has already happened and must not be undone by a failed message.
"""

import logging
import re

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.html import escape
from django.utils.safestring import mark_safe

from . import notify_deferred
from .models import NotificationEventRecipient, NotificationEventSetting, NotificationTemplate
from .notifications import _log, send_notification, valid_whatsapp_number
from .notify_registry import IDENTIFIER_RE, PLACEHOLDER_RE, WHATSAPP, EmailTable, get_event
from .whatsapp_params import describe_params, sanitise_params

logger = logging.getLogger(__name__)

# A paragraph break is a blank line, which may hold stray spaces.
_PARAGRAPH_SPLIT_RE = re.compile(r'\n[ \t]*\n')
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.S)

# Inline, because email clients strip <style>. Matches the layout's body cell.
_PARAGRAPH_STYLE = 'margin:0 0 14px 0;'

DEFAULT_BUTTON_LABEL = 'Open Horizon Solar PMS'


class RenderError(Exception):
    """A placeholder in a subject or body that the event does not supply."""


def absolute_url(path):
    """`path` on APP_BASE_URL; an absolute URL is returned as is, '' stays ''.

    The one place notify() and its previews build a link, so a domain change is one
    setting. Read at call time, not import time, so override_settings reaches it.
    """
    if not path:
        return ''
    if path.startswith(('http://', 'https://')):
        return path
    return f"{settings.APP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _substitute(text, values, allowed, transform):
    """Replace every {{ name }} in `text` with transform(values[name]).

    `allowed` is the event's variable names. A name outside it is refused even when the
    caller happens to pass a value for it, so a template can only ever print what the
    event declares.
    """
    def replace(match):
        name = match.group(1).strip()
        if not IDENTIFIER_RE.match(name) or name not in allowed:
            raise RenderError(f'Unknown placeholder {{{{ {name} }}}}')
        if name not in values:
            raise RenderError(f'No value for placeholder {{{{ {name} }}}}')
        return transform(values[name])
    return PLACEHOLDER_RE.sub(replace, text or '')


def _paragraphs(body):
    body = (body or '').replace('\r\n', '\n')
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]


def render_plain(text, values, allowed):
    """One line of plain text — a subject or the in-app text. Whitespace runs collapse,
    so a value holding a newline cannot break an email header."""
    return ' '.join(_substitute(text, values, allowed, str).split())


def _renders_empty(paragraph, values, allowed):
    """Whether `paragraph` is blank once filled: a paragraph that is only an optional
    value, such as an approval's summary line, when that value is ''. Such a paragraph is
    dropped from both parts (T3c, Q3), so an empty value leaves no blank gap and no empty
    <p>. Raises RenderError as the renderers do."""
    return not _substitute(_BOLD_RE.sub(r'\1', paragraph), values, allowed, str).strip()


# --- E8 (NTF-DCG 5c, N7): the server-built table block --------------------------------
# A paragraph that is exactly one placeholder whose VALUE is an EmailTable is drawn as a
# table. Nothing else changes: only a call site that passes an EmailTable can reach this
# branch, and none did before 5c, so every existing email renders byte for byte as it
# did (tests_grn_notify compares all of them with the code before 5c's hashes).
_WHOLE_PLACEHOLDER_RE = re.compile(r'^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$')

# Inline, because email clients strip <style>. Sized for the layout's 600px body cell
# and to wrap, not scroll, on a phone: three columns, cells break long words.
_TABLE_STYLE = ('width:100%; border-collapse:collapse; margin:0 0 14px 0; '
                'font-size:13px; line-height:1.4;')
_TH_STYLE = ('text-align:left; vertical-align:bottom; padding:6px 6px; color:#6b7785; '
             'font-weight:bold; border-bottom:2px solid #e5e9ee;')
_TD_STYLE = ('text-align:left; vertical-align:top; padding:6px 6px; '
             'border-bottom:1px solid #e5e9ee; word-break:break-word;')
_TABLE_LINE_STYLE = 'margin:0 0 6px 0;'


def _table_value(paragraph, values, allowed):
    """The EmailTable `paragraph` places, or None. Only a paragraph that is nothing but
    one allowed placeholder qualifies; a table named inside a sentence renders as its
    text, escaped, through the ordinary path."""
    match = _WHOLE_PLACEHOLDER_RE.match(paragraph)
    if match is None or match.group(1) not in allowed:
        return None
    value = values.get(match.group(1))
    return value if isinstance(value, EmailTable) else None


def render_table_html(table):
    """`table` as HTML. EVERY string in it — caption, header, cell, footer — is escaped
    before a tag is put round it, and a cell's '\n' becomes <br> only after that, so a
    description holding markup prints as text. Returns a str; render_body_html() marks
    the whole body safe, which this justifies."""
    def cell(text):
        return str(escape(str(text))).replace('\r\n', '\n').replace('\n', '<br>\n')

    parts = []
    if table.caption:
        parts.append(f'<p style="{_TABLE_LINE_STYLE}"><strong>{cell(table.caption)}</strong></p>')
    parts.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 f'style="{_TABLE_STYLE}">')
    parts.append('<tr>' + ''.join(f'<th style="{_TH_STYLE}">{cell(c)}</th>'
                                  for c in table.columns) + '</tr>')
    for row in table.rows:
        parts.append('<tr>' + ''.join(f'<td style="{_TD_STYLE}">{cell(c)}</td>'
                                      for c in row) + '</tr>')
    parts.append('</table>')
    parts += [f'<p style="{_TABLE_LINE_STYLE}">{cell(line)}</p>' for line in table.footer]
    # The paragraph margin after the block, as every <p> of the body has.
    return f'<div style="{_PARAGRAPH_STYLE}">{"".join(parts)}</div>'


def render_body_text(body, values, allowed):
    """The plain-text body: paragraphs separated by one blank line, **markers** dropped,
    a paragraph that fills to nothing left out. A table paragraph (E8) prints the
    table's text()."""
    out = []
    for p in _paragraphs(body):
        if _renders_empty(p, values, allowed):
            continue
        table = _table_value(p, values, allowed)
        out.append(table.text() if table is not None
                   else _substitute(_BOLD_RE.sub(r'\1', p), values, allowed, str))
    return '\n\n'.join(out)


def render_body_html(body, values, allowed):
    """The HTML body: one <p> per paragraph, a single line break as <br>, **text** as
    <strong>, nothing else (T2, A8 added the <br>; the text body keeps the break as is). A
    paragraph that fills to nothing is left out, as in the text body (T3c, Q3).

    The ORDER is what makes it safe. The Admin-typed paragraph is escaped first (escape()
    leaves { } and * alone, so placeholders and bold markers survive it); bold and line
    breaks are applied to that, before any value is in it, so a value holding ** cannot
    become markup; each value is escaped as it is substituted, and only then are its own
    line breaks turned into <br> (T3a, P3: a multi-line value such as an address reads as
    lines, as it does in the text body). Every character in the result is therefore either
    escaped input or a tag written here, which is what justifies mark_safe.

    E8 (NTF-DCG 5c): a paragraph that only places an EmailTable value is drawn by
    render_table_html(), which escapes every string in the table the same way.
    """
    def fill(value):
        text = str(escape(str(value))).replace('\r\n', '\n')
        return text.replace('\n', '<br>\n')

    out = []
    for paragraph in _paragraphs(body):
        if _renders_empty(paragraph, values, allowed):
            continue
        table = _table_value(paragraph, values, allowed)
        if table is not None:
            out.append(render_table_html(table))
            continue
        marked = _BOLD_RE.sub(r'<strong>\1</strong>', str(escape(paragraph)))
        marked = marked.replace('\n', '<br>\n')
        filled = _substitute(marked, values, allowed, fill)
        out.append(f'<p style="{_PARAGRAPH_STYLE}">{filled}</p>')
    return mark_safe(''.join(out))


def render_email(event, template, values, link):
    """(subject, text, html) for one email of `event`.

    `template` is a NotificationTemplate, or None for the code default. `values` must
    already hold recipient_name; the absolute link is added here from `link`. Raises
    RenderError when a placeholder cannot be filled.
    """
    values = dict(values)
    values['link'] = absolute_url(link)
    allowed = event.variable_names
    if template is not None:
        subject_src, body_src, button = template.subject, template.body, template.button_label
    else:
        subject_src, body_src, button = (event.default_subject, event.default_body,
                                         event.button_label)
    context = {
        'subject': render_plain(subject_src, values, allowed),
        'body_html': render_body_html(body_src, values, allowed),
        'body_text': render_body_text(body_src, values, allowed),
        'link': values['link'],
        'button_label': button or DEFAULT_BUTTON_LABEL,
        # The footer names the address _zeptomail_post sets as reply-to (T3a, P4); both
        # read the one setting, so the footer cannot name a different inbox.
        'reply_to': settings.NOTIFY_REPLY_TO,
    }
    html = render_to_string('projects/email/base_email.html', context)
    text = render_to_string('projects/email/base_email.txt', context)
    return context['subject'], text, html


def channel_is_active(event, channel):
    """Whether `event` may send on `channel`: the setting row, else the registry default
    for that channel (default_state_for: WhatsApp is always paused until switched on)."""
    setting = NotificationEventSetting.objects.filter(
        event_code=event.code, channel=channel).only('is_active').first()
    if setting is None:
        return event.default_state_for(channel) == 'active'
    return setting.is_active


def active_template(event_code, channel):
    """The ACTIVE NotificationTemplate for the event and channel, or None (code default)."""
    return NotificationTemplate.objects.filter(
        event_code=event_code, channel=channel, status=NotificationTemplate.ACTIVE).first()


def resolve_recipients(recipients, actor):
    """`recipients` de-duplicated by pk, without the actor or anyone deactivated.

    Both active flags count: UserProfile.is_active is this app's soft deactivation, and
    User.is_active is Django's; either one off means the person no longer works here.

    PUBLIC because a call site that still sends WhatsApp itself (T3a: WhatsApp moves in
    its own phase) must use this for that leg too (ruling Q3), so in-app, email and
    WhatsApp always reach the same people and cannot drift apart.
    """
    actor_pk = getattr(actor, 'pk', None)
    seen, out = set(), []
    for profile in recipients or ():
        if profile is None or profile.pk in seen:
            continue
        seen.add(profile.pk)
        if profile.pk == actor_pk:
            continue
        if not (profile.is_active and profile.user.is_active):
            continue
        out.append(profile)
    return out


INACTIVE_NAMED_RECIPIENT = 'inactive'


def merge_named_recipients(event, targets):
    """(targets + the event's active named recipients, inactive named recipients), for an
    event registered with named_recipients=True (NTF-DCG 5b, N3). Read at send time.

    De-duplicated by profile: a person already in `targets` (code-resolved) keeps that
    place and gets no second send. Named recipients are NOT filtered by the actor (ruling
    5b-Q2: the list is a record trail, so the person Admin named hears even about their
    own act) and face no permission check (Admin chose them). A deactivated one is
    returned in the second list so notify_now() can log why they got nothing (ruling
    5b-Q3), instead of being dropped silently as resolve_recipients() drops a
    code-resolved one.
    """
    seen = {p.pk for p in targets}
    merged, inactive = list(targets), []
    # The active rows of this event, oldest first, with each person's user loaded: the
    # active flags are read here and the name and address in _send_to_one().
    rows = (NotificationEventRecipient.objects
            .filter(event_code=event.code, removed_at__isnull=True)
            .select_related('recipient__user').order_by('added_at', 'pk'))
    for row in rows:
        profile = row.recipient
        if profile.pk in seen:
            continue
        seen.add(profile.pk)
        if profile.is_active and profile.user.is_active:
            merged.append(profile)
        else:
            inactive.append(profile)
    return merged, inactive


def _log_inactive_named(event, profile, actor, related_project, log_label):
    """One 'skipped' row per channel a deactivated named recipient would have reached:
    in-app and each declared channel. "inactive" wins over "event paused" (ruling 5b-Q3),
    so these rows are written whatever the channels' switches say."""
    base = _log_base(event, profile, actor, related_project, log_label)
    _log(base, 'in_app', 'skipped', INACTIVE_NAMED_RECIPIENT, event_code=event.code)
    for channel in event.channels:
        row_base = base
        if channel == WHATSAPP and event.whatsapp is not None:
            # As _whatsapp_leg's rows: a WhatsApp row names the Interakt template.
            row_base = dict(base, template_name=event.whatsapp.template)
        _log(row_base, channel, 'skipped', INACTIVE_NAMED_RECIPIENT, event_code=event.code)


def _log_base(event, recipient, actor, related_project, log_label):
    """The fixed fields of a row notify() writes itself. `message` is the event's label:
    a paused or failed send has no rendered text to record."""
    return dict(recipient=recipient, related_project=related_project, actor=actor,
                message=event.label, template_name=log_label or '')


def _email_plan(event):
    """What every recipient's email does, decided once per send: ('paused', None),
    ('send', template-or-None), or ('error', exception)."""
    try:
        if not channel_is_active(event, 'email'):
            return 'paused', None
        return 'send', active_template(event.code, 'email')
    except Exception as exc:
        logger.exception('notify: could not read the email state of %s', event.code)
        return 'error', exc


def _whatsapp_plan(event):
    """What every recipient's WhatsApp does, decided once per send, as _email_plan:
    ('paused', None), ('send', event.whatsapp) or ('error', exception)."""
    try:
        if not channel_is_active(event, WHATSAPP):
            return 'paused', None
        return 'send', event.whatsapp
    except Exception as exc:
        logger.exception('notify: could not read the WhatsApp state of %s', event.code)
        return 'error', exc


def _send_to_one(event, recipient, values, actor, related_project, link, plan, log_label,
                 wa_plan=None, wa_seen=None):
    """In-app, then email, then WhatsApp, for one person. The channels are independent
    (ruling Q4): a failure on one is logged and the others are still attempted.

    `log_label` reaches send_notification() as `template`. On the in_app and email
    channels that argument is used for one thing only, NotificationLog.template_name, so
    this is how the label lands on the rows without a change to send_notification().

    `wa_plan` is _whatsapp_plan()'s answer, None for an event without a WhatsApp leg.
    `wa_seen` is the set of numbers already handed off in this send (W9), shared by
    every recipient of one notify() call.
    """
    name = recipient.user.get_full_name() or recipient.user.username
    # NTF-DCG 3b (Q3): the first name, for a WhatsApp greeting. A key no other event
    # declares, so their rendering cannot read it and is byte-identical.
    first_name = recipient.user.first_name.strip() or recipient.user.username
    values = dict(values, recipient_name=name, recipient_first_name=first_name,
                  link=absolute_url(link))
    allowed = event.variable_names
    base = _log_base(event, recipient, actor, related_project, log_label)

    try:
        send_notification(
            recipient=recipient,
            message=render_plain(event.in_app_text, values, allowed),
            channels=['in_app'],
            link=link,
            template=log_label,
            related_project=related_project,
            actor=actor,
            event_code=event.code,
        )
    except Exception as exc:
        logger.exception('notify: in-app %s to %s failed', event.code, recipient.pk)
        _log(base, 'in_app', 'failed', f'{type(exc).__name__}: {exc}',
             event_code=event.code)

    if plan is not None:
        _email_leg(event, recipient, values, actor, related_project, link, plan,
                   log_label, base)
    if wa_plan is not None:
        _whatsapp_leg(event, recipient, values, wa_plan, base,
                      set() if wa_seen is None else wa_seen)


def _email_leg(event, recipient, values, actor, related_project, link, plan, log_label,
               base):
    """The email of one person. The body _send_to_one() always had for it, moved here
    unchanged (S3a) so that its early returns end the email leg only, not WhatsApp."""
    state, payload = plan
    if state == 'paused':
        _log(base, 'email', 'skipped', 'event paused', event_code=event.code)
        return
    if state == 'error':
        _log(base, 'email', 'failed', f'{type(payload).__name__}: {payload}',
             event_code=event.code)
        return
    try:
        subject, text, html = render_email(event, payload, values, link)
        if event.deferred:
            # NTF-DCG S2: the same rendered email, sent from the background pool. The
            # pending row carries what send_notification() would have logged; the job
            # finishes it. Only the provider call leaves this thread.
            row_fields = dict(base, message=text, event_code=event.code)
            if payload is not None:
                row_fields['template_version'] = payload
            notify_deferred.defer(
                recipient=recipient, channel='email', row_fields=row_fields,
                send_kwargs=dict(message=text, channels=['email'], subject=subject,
                                 html_message=html, template=log_label))
            return
        send_notification(
            recipient=recipient,
            message=text,
            channels=['email'],
            subject=subject,
            html_message=html,
            template=log_label,
            related_project=related_project,
            actor=actor,
            event_code=event.code,
            template_version=payload,
        )
    except Exception as exc:
        logger.exception('notify: email %s to %s failed', event.code, recipient.pk)
        _log(base, 'email', 'failed', f'{type(exc).__name__}: {exc}',
             event_code=event.code, template_version=payload)


def _whatsapp_leg(event, recipient, values, plan, base, seen):
    """The WhatsApp of one person (NTF-DCG S3a, W2-W5, W9). Never raises.

    Paused ⇒ a 'skipped' row and nothing else, as for email. Otherwise the builder's
    values are checked against param_count, sanitised (W4) and handed to the deferred
    pool with ONE pending row; the job, inside send_notification(), applies the master
    switch, the person's preference and the strict number check (W5), then posts.

    The row's template_name is the Interakt template and its message the exact values
    sent, header and body marked (ruling Q7): the in-app row already holds the in-app
    text, and this is what the person's phone shows.
    """
    state, payload = plan
    wa_base = dict(base, template_name=event.whatsapp.template)
    if state == 'paused':
        _log(wa_base, WHATSAPP, 'skipped', 'event paused', event_code=event.code)
        return
    if state == 'error':
        _log(wa_base, WHATSAPP, 'failed', f'{type(payload).__name__}: {payload}',
             event_code=event.code)
        return
    spec = payload
    try:
        # W9: one message per phone per send. Two profiles sharing a number (a test
        # handset, a family phone) would otherwise get the same WhatsApp twice. Read at
        # hand-off from the profile notify() was given; the job re-reads the number and
        # checks it (W5). An invalid number is never "seen", so each such person still
        # gets their own 'failed' row from the job.
        digits = valid_whatsapp_number(recipient.phone_number)
        if digits is not None:
            if digits in seen:
                _log(wa_base, WHATSAPP, 'skipped', 'duplicate phone in this send',
                     event_code=event.code)
                return
            seen.add(digits)
        raw = list(spec.params(values))
        if len(raw) != spec.param_count:
            raise ValueError(f'params builder returned {len(raw)} values, '
                             f'param_count is {spec.param_count}')
        params = sanitise_params(raw, header_fixed_chars=spec.header_fixed_chars,
                                 body_fixed_chars=spec.body_fixed_chars)
        text = describe_params(params)
        notify_deferred.defer(
            recipient=recipient, channel=WHATSAPP,
            row_fields=dict(wa_base, message=text, event_code=event.code),
            send_kwargs=dict(message=text, channels=[WHATSAPP], template=spec.template,
                             template_params=params, whatsapp_language=spec.language,
                             whatsapp_strict_phone=True))
    except Exception as exc:
        logger.exception('notify: WhatsApp %s to %s failed', event.code, recipient.pk)
        _log(wa_base, WHATSAPP, 'failed', f'{type(exc).__name__}: {exc}',
             event_code=event.code)


def _send(event, recipients, values, actor, related_project, link, log_label):
    plan = _email_plan(event) if 'email' in event.channels else None
    wa_plan = _whatsapp_plan(event) if event.whatsapp is not None else None
    wa_seen = set()   # W9: numbers already handed off in THIS send
    for recipient in recipients:
        try:
            _send_to_one(event, recipient, values, actor, related_project, link, plan,
                         log_label, wa_plan, wa_seen)
        except Exception:
            # _send_to_one already catches per channel; this is the backstop for
            # anything outside those (reading the recipient's name, say), so one
            # person's fault never costs the next person their message.
            logger.exception('notify: %s to %s failed', event.code, recipient.pk)


def notify_now(event_code, *, recipients, values, actor=None, related_project=None, link='',
               log_label=None):
    """notify(), sent NOW rather than after commit: the same recipients rule, rendering,
    channels and log rows.

    FOR A CALLER THAT IS ALREADY RUNNING ON COMMIT (T3b, ruling Q3). The assignment
    cooldown has to count, decide and send inside ONE after-commit callback. Had it called
    notify() there, the send would be registered as a second callback: Django runs that at
    once in production, but captureOnCommitCallbacks queues it behind the outer callbacks,
    so a second assignment in the same transaction would count zero rows and send
    assign_task twice in tests while production sent assign_tasks_bulk. Called inside an
    open transaction, this sends before that transaction's outcome is known, which is
    exactly what notify() exists to prevent; use notify() everywhere else.

    Raises KeyError for an unknown event code. Nothing after that raises.

    NAMED RECIPIENTS (NTF-DCG 5b, N3) are merged HERE, the one place every send of every
    channel passes through: notify() reaches it after commit, the assignment cooldown
    calls it directly. _send() then takes each merged person through in-app, email and
    WhatsApp, deferred or not, exactly as a code-resolved one.
    """
    event = get_event(event_code)
    try:
        targets = resolve_recipients(recipients, actor)
        if event.named_recipients:
            try:
                targets, inactive = merge_named_recipients(event, targets)
            except Exception:
                # Reading the list failed: the code-resolved people still get the event.
                logger.exception('notify_now: could not read named recipients of %s',
                                 event_code)
                inactive = []
            for profile in inactive:
                try:
                    _log_inactive_named(event, profile, actor, related_project, log_label)
                except Exception:
                    logger.exception('notify_now: could not log inactive %s', profile.pk)
        if targets:
            _send(event, targets, dict(values or {}), actor, related_project, link,
                  log_label)
    except Exception:
        logger.exception('notify_now: %s failed', event_code)


def notify(event_code, *, recipients, values, actor=None, related_project=None, link='',
           log_label=None):
    """Send `event_code` to `recipients` after the current transaction commits.

    recipients      : iterable of UserProfile. De-duplicated; the actor and anyone
                      deactivated are dropped (resolve_recipients).
    values          : {variable name: value} for the event's declared variables.
                      recipient_name and link are supplied here and override any given.
    actor           : UserProfile who caused the event; never told about their own act.
    related_project : stored on every log row.
    link            : relative path for the in-app click-through; the email's button
                      links to absolute_url(link). '' means no button.
    log_label       : written to NotificationLog.template_name on every row this send
                      causes, in-app and email, sent or not (T3a, P1). A send moved onto
                      notify() passes the label it logged before, so the readers and
                      filters keyed on that column keep working; event_code is recorded
                      beside it either way. None leaves the column blank.

    Raises KeyError for an unknown event code (a programmer error, caught by any test
    that fires the event). Nothing after that raises.

    The body is notify_now(), registered on commit (T3b). The recipients are resolved
    HERE, before the callback, as before T3b, so who is told is decided by the state at
    the time of the act. notify_now() resolves the same profile objects again, which reads
    the flags already loaded and drops nobody new.
    """
    event = get_event(event_code)
    try:
        targets = resolve_recipients(recipients, actor)
        # NTF-DCG 5b (ruling 5b-Q4): an opted-in event is scheduled even when its code
        # resolves nobody, because its named recipients are read in notify_now().
        if not targets and not event.named_recipients:
            return
        frozen = dict(values or {})
        # After commit, so a rolled-back action sends nothing; outside a transaction
        # Django runs it at once. Robust, so a failure here is logged and never reaches
        # the committed action. A lambda, not functools.partial: robust on_commit names
        # the callback in its log line, which needs a __qualname__.
        transaction.on_commit(
            lambda: notify_now(event_code, recipients=targets, values=frozen, actor=actor,
                               related_project=related_project, link=link,
                               log_label=log_label),
            robust=True)
    except Exception:
        logger.exception('notify: could not schedule %s', event_code)
