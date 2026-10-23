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

    # --- T3a: existing sends moved onto managed templates -------------------------
    # Wording is the code's wording at T3a, pinned by tests_notify_migration_a. The
    # "View in Horizon Solar PMS" line those emails ended with is now the button. Only
    # 'email' is declared: these events' WhatsApp messages are still sent directly by
    # their call sites, with resolve_recipients(), until the WhatsApp phase moves them,
    # so no Admin switch here can appear to pause a WhatsApp it does not control.
    NotificationEvent(
        code='issue_raised',
        label='Issue raised',
        module='Issues',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('issue_title', 'Title of the issue', 'Inverter not powering on'),
            Variable('raised_by_name', 'The person who raised the issue', 'Rahul Verma'),
        ),
        in_app_text=('A new issue has been raised on project {{ customer_name }}: '
                     '"{{ issue_title }}". Please login to review and coordinate '
                     'resolution.'),
        default_subject='New Issue Raised — {{ customer_name }}',
        default_body=(
            'A new issue has been raised on project {{ customer_name }}: '
            '"{{ issue_title }}".\n\n'
            'Please login to review and coordinate resolution.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('The person the issue is assigned to, unless they raised it. '
                         'The project managers get a separate in-app note only.'),
        default_state='active',
    ),
    NotificationEvent(
        code='issue_resolved',
        label='Issue resolved',
        module='Issues',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('issue_title', 'Title of the issue', 'Inverter not powering on'),
            Variable('resolved_by_name', 'The person who marked it resolved', 'Rahul Verma'),
        ),
        in_app_text=('The issue "{{ issue_title }}" on project {{ customer_name }} has '
                     'been marked as resolved by {{ resolved_by_name }}.'),
        default_subject='Issue Resolved — {{ customer_name }}',
        default_body=(
            'The issue "{{ issue_title }}" on project {{ customer_name }} has been marked '
            'as resolved by {{ resolved_by_name }}.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('The project managers (PM and coordinators), the person the issue '
                         'is assigned to and the person who raised it, except the resolver.'),
        default_state='active',
    ),
    NotificationEvent(
        code='project_pm_assigned',
        label='PM assigned to a project',
        module='Projects',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('customer_and_city',
                     'Customer name, with the city in brackets when the project has one',
                     'Sharma Residence (Lucknow)'),
        ),
        in_app_text=('Hi {{ recipient_name }}, You have been assigned as Project Manager '
                     'for {{ customer_and_city }}. Please review the project details and '
                     'activate when ready.'),
        default_subject='New Project Assigned: {{ customer_name }}',
        default_body=(
            'Hi {{ recipient_name }},\n\n'
            'You have been assigned as Project Manager for {{ customer_and_city }}.\n\n'
            'Please review the project details and activate when ready.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('The PM given a project with no PM, by an Admin or by the Zoho '
                         'CRM deal. A change of PM sends the handover notices instead.'),
        default_state='active',
    ),
    NotificationEvent(
        code='project_unassigned_alert',
        label='Zoho project with no PM',
        module='Projects',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('city', 'City of the project, or a dash when Zoho sent none', 'Lucknow'),
            Variable('deal_id', 'The Zoho CRM deal ID', '5725767000001234567'),
            Variable('zoho_pm_email',
                     'The PM email Zoho sent, or "(blank)" when it sent none',
                     'pm.name@horizonrenewablepower.com'),
        ),
        in_app_text=('New Draft project {{ project_id }} created from Zoho CRM (deal '
                     '{{ deal_id }}) — PM could not be assigned. Please assign a PM.'),
        default_subject='[SolarPMS] Unassigned Project: {{ project_id }}',
        default_body=(
            'A new Draft project was created via Zoho CRM but no PM could be assigned.\n\n'
            'Project ID : {{ project_id }}\n'
            'Customer   : {{ customer_name }}\n'
            'City       : {{ city }}\n'
            'Zoho Deal  : {{ deal_id }}\n'
            'PM email from Zoho: {{ zoho_pm_email }}\n\n'
            'Please log in to the Admin Panel and assign a PM:'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule='Every active Admin.',
        default_state='active',
    ),
    NotificationEvent(
        code='boq_acknowledged',
        label='BOQ acknowledged by SCM',
        module='BOQ',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('acknowledged_by_name', 'The SCM person who acknowledged the BOQ',
                     'Anil Gupta'),
        ),
        in_app_text=('The Bill of Quantities for project {{ customer_name }} has been '
                     'acknowledged by {{ acknowledged_by_name }}. Material procurement can '
                     'now proceed.'),
        default_subject='BOQ Acknowledged — {{ customer_name }}',
        default_body=(
            'The Bill of Quantities for project {{ customer_name }} has been acknowledged '
            'by {{ acknowledged_by_name }}.\n\n'
            'Material procurement can now proceed.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule='The project managers (PM and coordinators).',
        default_state='active',
    ),
    NotificationEvent(
        code='payment_milestone_reached',
        label='Payment milestone reached',
        module='Payments',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('task_name', 'The milestone task that was completed',
                     'Plant Commissioning'),
        ),
        in_app_text=('Project {{ project_id }} ({{ customer_name }}) has reached payment '
                     'milestone: "{{ task_name }}". Please initiate collection at the '
                     'earliest.'),
        default_subject='Payment Milestone Reached — {{ customer_name }}',
        default_body=(
            'Project {{ project_id }} ({{ customer_name }}) has reached payment milestone: '
            '"{{ task_name }}".\n\n'
            'Please initiate collection at the earliest.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('Finance, BD and CEO users and the project managers (PM and '
                         'coordinators), except the person who completed the task.'),
        default_state='active',
    ),
])


def get_event(event_code):
    """The registered event, or KeyError — an unknown code is a programmer error."""
    try:
        return NOTIFICATION_EVENTS[event_code]
    except KeyError:
        raise KeyError(f'Unknown notification event {event_code!r}') from None
