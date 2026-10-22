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

from .models import NotificationEventSetting, NotificationTemplate
from .notifications import _log, send_notification
from .notify_registry import IDENTIFIER_RE, PLACEHOLDER_RE, get_event

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


def render_body_text(body, values, allowed):
    """The plain-text body: paragraphs separated by one blank line, **markers** dropped."""
    return '\n\n'.join(
        _substitute(_BOLD_RE.sub(r'\1', p), values, allowed, str) for p in _paragraphs(body))


def render_body_html(body, values, allowed):
    """The HTML body: one <p> per paragraph, **text** as <strong>, nothing else.

    The ORDER is what makes it safe. The Admin-typed paragraph is escaped first (escape()
    leaves { } and * alone, so placeholders and bold markers survive it); bold is applied
    to that, before any value is in it, so a value holding ** cannot become markup; each
    value is escaped as it is substituted. Every character in the result is therefore
    either escaped input or a tag written here, which is what justifies mark_safe.
    """
    out = []
    for paragraph in _paragraphs(body):
        marked = _BOLD_RE.sub(r'<strong>\1</strong>', str(escape(paragraph)))
        filled = _substitute(marked, values, allowed, lambda v: str(escape(str(v))))
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
    }
    html = render_to_string('projects/email/base_email.html', context)
    text = render_to_string('projects/email/base_email.txt', context)
    return context['subject'], text, html


def channel_is_active(event, channel):
    """Whether `event` may send on `channel`: the setting row, else the registry default."""
    setting = NotificationEventSetting.objects.filter(
        event_code=event.code, channel=channel).only('is_active').first()
    if setting is None:
        return event.default_state == 'active'
    return setting.is_active


def active_template(event_code, channel):
    """The ACTIVE NotificationTemplate for the event and channel, or None (code default)."""
    return NotificationTemplate.objects.filter(
        event_code=event_code, channel=channel, status=NotificationTemplate.ACTIVE).first()


def _eligible(recipients, actor):
    """`recipients` de-duplicated by pk, without the actor or anyone deactivated.

    Both active flags count: UserProfile.is_active is this app's soft deactivation, and
    User.is_active is Django's; either one off means the person no longer works here.
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


def _log_base(event, recipient, actor, related_project):
    """The fixed fields of a row notify() writes itself. `message` is the event's label:
    a paused or failed send has no rendered text to record."""
    return dict(recipient=recipient, related_project=related_project, actor=actor,
                message=event.label, template_name='')


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


def _send_to_one(event, recipient, values, actor, related_project, link, plan):
    """In-app, then email, for one person. The channels are independent (ruling Q4): a
    failure on one is logged and the other is still attempted."""
    name = recipient.user.get_full_name() or recipient.user.username
    values = dict(values, recipient_name=name, link=absolute_url(link))
    allowed = event.variable_names

    try:
        send_notification(
            recipient=recipient,
            message=render_plain(event.in_app_text, values, allowed),
            channels=['in_app'],
            link=link,
            related_project=related_project,
            actor=actor,
            event_code=event.code,
        )
    except Exception as exc:
        logger.exception('notify: in-app %s to %s failed', event.code, recipient.pk)
        _log(_log_base(event, recipient, actor, related_project), 'in_app', 'failed',
             f'{type(exc).__name__}: {exc}', event_code=event.code)

    if plan is None:
        return
    state, payload = plan
    base = _log_base(event, recipient, actor, related_project)
    if state == 'paused':
        _log(base, 'email', 'skipped', 'event paused', event_code=event.code)
        return
    if state == 'error':
        _log(base, 'email', 'failed', f'{type(payload).__name__}: {payload}',
             event_code=event.code)
        return
    try:
        subject, text, html = render_email(event, payload, values, link)
        send_notification(
            recipient=recipient,
            message=text,
            channels=['email'],
            subject=subject,
            html_message=html,
            related_project=related_project,
            actor=actor,
            event_code=event.code,
            template_version=payload,
        )
    except Exception as exc:
        logger.exception('notify: email %s to %s failed', event.code, recipient.pk)
        _log(base, 'email', 'failed', f'{type(exc).__name__}: {exc}',
             event_code=event.code, template_version=payload)


def _send(event, recipients, values, actor, related_project, link):
    plan = _email_plan(event) if 'email' in event.channels else None
    for recipient in recipients:
        try:
            _send_to_one(event, recipient, values, actor, related_project, link, plan)
        except Exception:
            # _send_to_one already catches per channel; this is the backstop for
            # anything outside those (reading the recipient's name, say), so one
            # person's fault never costs the next person their message.
            logger.exception('notify: %s to %s failed', event.code, recipient.pk)


def notify(event_code, *, recipients, values, actor=None, related_project=None, link=''):
    """Send `event_code` to `recipients` after the current transaction commits.

    recipients      : iterable of UserProfile. De-duplicated; the actor and anyone
                      deactivated are dropped.
    values          : {variable name: value} for the event's declared variables.
                      recipient_name and link are supplied here and override any given.
    actor           : UserProfile who caused the event; never told about their own act.
    related_project : stored on every log row.
    link            : relative path for the in-app click-through; the email's button
                      links to absolute_url(link). '' means no button.

    Raises KeyError for an unknown event code (a programmer error, caught by any test
    that fires the event). Nothing after that raises.
    """
    event = get_event(event_code)
    try:
        targets = _eligible(recipients, actor)
        if not targets:
            return
        frozen = dict(values or {})
        # After commit, so a rolled-back action sends nothing; outside a transaction
        # Django runs it at once. Robust, so a failure here is logged and never reaches
        # the committed action. A lambda, not functools.partial: robust on_commit names
        # the callback in its log line, which needs a __qualname__.
        transaction.on_commit(
            lambda: _send(event, targets, frozen, actor, related_project, link),
            robust=True)
    except Exception:
        logger.exception('notify: could not schedule %s', event_code)
