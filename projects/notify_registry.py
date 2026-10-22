"""
Notification event registry (T1, Notification Template Management spec v0.1).

Every event notify.notify() can send is declared here, in code. Admin may change an
event's email wording (NotificationTemplate) and switch a channel on or off
(NotificationEventSetting); Admin may NOT add an event, rename one, change who receives
it, or change its in-app text. Those are code changes, because the call site that fires
the event is code.

`recipients_rule` is documentation shown to Admin. It is not executed: each call site
resolves its recipients with the permissions.py helpers and passes them to notify().

IMPORT-TIME CHECK. _build_registry() refuses duplicate codes and any placeholder in the
in-app text, default subject or default body that the event does not declare, so a code
default that cannot render never reaches a deploy. `link` and `recipient_name` are
reserved: notify() supplies them for every send, and no event may declare them.

This module imports nothing from the app, so models.py, notify.py and notify_admin.py
can all import it without a cycle.
"""

import re
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

TIER_ACT  = 'ACT'    # the recipient has something to do
TIER_KNOW = 'KNOW'   # the recipient is being told something
TIERS = (TIER_ACT, TIER_KNOW)

# Mirrors models.NOTIFY_CHANNEL_* and NOTIFY_STATE_*; spelled out rather than imported
# so this module stays free of app imports (see the module docstring).
CHANNELS = ('email', 'whatsapp')
STATES = ('active', 'paused')

# Supplied by notify() for every send; always available to every template.
RESERVED_VARIABLES = ('link', 'recipient_name')
RESERVED_SAMPLES = {'recipient_name': 'Priya Sharma', 'link': '/'}

# Anything between {{ and }}. Deliberately broader than a valid placeholder: the
# renderer and validate_template() need to SEE `{{ request.user }}` to refuse it, which
# a strict identifier pattern would silently pass over as literal text.
PLACEHOLDER_RE = re.compile(r'\{\{(.*?)\}\}', re.S)
IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass(frozen=True)
class Variable:
    name: str
    meaning: str   # one line shown to Admin beside the placeholder
    sample: str    # used by the preview, never by a real send


@dataclass(frozen=True)
class NotificationEvent:
    code: str
    label: str
    module: str
    tier: str
    channels: tuple
    variables: tuple
    in_app_text: str
    default_subject: str
    default_body: str
    button_label: str
    recipients_rule: str
    default_state: str

    @property
    def variable_names(self):
        """Declared names plus the reserved ones: everything a template may use."""
        return tuple(v.name for v in self.variables) + RESERVED_VARIABLES

    def samples(self):
        """{name: sample} for every available name, for the preview."""
        values = dict(RESERVED_SAMPLES)
        values.update({v.name: v.sample for v in self.variables})
        return values


def placeholder_names(text):
    """The stripped contents of every {{ ... }} in `text`, in order, repeats kept."""
    return [m.group(1).strip() for m in PLACEHOLDER_RE.finditer(text or '')]


def _build_registry(events):
    """{code: event} from `events`, or ImproperlyConfigured naming every fault found."""
    errors, registry = [], {}
    for event in events:
        where = f'notification event {event.code!r}'
        if event.code in registry:
            errors.append(f'{where}: duplicate code')
        if not IDENTIFIER_RE.match(event.code) or event.code != event.code.lower():
            errors.append(f'{where}: code must be snake_case')
        if event.tier not in TIERS:
            errors.append(f'{where}: tier must be one of {TIERS}')
        if event.default_state not in STATES:
            errors.append(f'{where}: default_state must be one of {STATES}')
        if not event.channels or any(c not in CHANNELS for c in event.channels):
            errors.append(f'{where}: channels must be a non-empty subset of {CHANNELS}')
        declared = [v.name for v in event.variables]
        for name in declared:
            if name in RESERVED_VARIABLES:
                errors.append(f'{where}: {name!r} is reserved and may not be declared')
            if not IDENTIFIER_RE.match(name):
                errors.append(f'{where}: variable {name!r} is not a valid name')
        if len(set(declared)) != len(declared):
            errors.append(f'{where}: a variable is declared twice')
        available = set(declared) | set(RESERVED_VARIABLES)
        for field in ('in_app_text', 'default_subject', 'default_body'):
            for name in placeholder_names(getattr(event, field)):
                if name not in available:
                    errors.append(f'{where}: {field} uses undeclared placeholder '
                                  f'{{{{ {name} }}}}')
        registry[event.code] = event
    if errors:
        raise ImproperlyConfigured('; '.join(errors))
    return registry


NOTIFICATION_EVENTS = _build_registry([
    # Admin's test send (T2): proves email reaches a person, and shows the layout.
    NotificationEvent(
        code='admin_test_message',
        label='Admin test message',
        module='Notifications',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('sender_name', 'The Admin who sent the test', 'Asha Rao'),
        ),
        in_app_text='Test message from {{ sender_name }}.',
        default_subject='Test message from Horizon Solar PMS',
        default_body=(
            'Hi {{ recipient_name }},\n\n'
            'This is a test message sent by **{{ sender_name }}** from the notification '
            'settings.\n\n'
            'If you are reading it, email from Horizon Solar PMS reaches you.'
        ),
        button_label='Open Horizon Solar PMS',
        recipients_rule='The person the Admin picks on the test-send screen.',
        default_state='active',
    ),
])


def get_event(event_code):
    """The registered event, or KeyError — an unknown code is a programmer error."""
    try:
        return NOTIFICATION_EVENTS[event_code]
    except KeyError:
        raise KeyError(f'Unknown notification event {event_code!r}') from None
