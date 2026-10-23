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
    # True when the call site also sends a WhatsApp message itself, outside notify(), so
    # pausing the event here pauses its email and not that WhatsApp (T3b, P2). The Admin
    # screens say so on the event. Goes when the WhatsApp phase moves those sends.
    whatsapp_sent_separately: bool = False

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
    # whatsapp_sent_separately (T3b, P2) marks the five whose call site does send one;
    # project_unassigned_alert has no WhatsApp leg.
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
        whatsapp_sent_separately=True,
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
        whatsapp_sent_separately=True,
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
        whatsapp_sent_separately=True,
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
        whatsapp_sent_separately=True,
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
        whatsapp_sent_separately=True,
    ),

    # --- T3b: task assignment --------------------------------------------------------
    # Wording pinned by tests_notify_migration_b. utils._notify_assignment chooses between
    # the two per recipient and project inside one after-commit callback (the cooldown),
    # and sends the WhatsApp leg itself, so both carry whatsapp_sent_separately.
    NotificationEvent(
        code='task_assigned',
        label='Task assigned',
        module='Tasks',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('task_name', 'Name of the task', 'Site Survey'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('assigner_name', 'The person who assigned the task', 'Rahul Verma'),
        ),
        in_app_text=('Hi {{ recipient_name }}, The task "{{ task_name }}" on project '
                     '{{ customer_name }} has been assigned to you. Please login to review '
                     'details and update progress.'),
        default_subject='Task Assigned: {{ task_name }} — {{ customer_name }}',
        default_body=(
            'Hi {{ recipient_name }},\n\n'
            'The task "{{ task_name }}" on project {{ customer_name }} has been assigned '
            'to you.\n\n'
            'Please login to review details and update progress.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('The person given a task on the assignment screens, or when a PM '
                         'adds a task, unless they assigned it to themself. Only their '
                         'first assignment on a project within an hour; the second sends '
                         '"Several tasks assigned" and later ones send nothing.'),
        default_state='active',
        whatsapp_sent_separately=True,
    ),
    NotificationEvent(
        code='tasks_assigned_bulk',
        label='Several tasks assigned',
        module='Tasks',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('task_count', 'How many tasks were assigned within the hour', '2'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('assigner_name', 'The person who made the latest assignment',
                     'Rahul Verma'),
        ),
        in_app_text=('Hi {{ recipient_name }}, {{ task_count }} tasks on project '
                     '{{ customer_name }} have been assigned to you. Please login to review '
                     'details and update progress.'),
        default_subject='{{ task_count }} Tasks Assigned — {{ customer_name }}',
        default_body=(
            'Hi {{ recipient_name }},\n\n'
            '{{ task_count }} tasks on project {{ customer_name }} have been assigned to '
            'you.\n\n'
            'Please login to review details and update progress.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('Sent instead of "Task assigned" for a person\'s second '
                         'assignment on the same project within an hour. The link opens '
                         'the project, not a task.'),
        default_state='active',
        whatsapp_sent_separately=True,
    ),

    # --- T3b: design and BOQ change requests --------------------------------------------
    # One event per log label, except the forward, which has two wordings (ruling Q4).
    # `noun` is "design change request" (raised by a PM or coordinator) or "BOQ change
    # request" (raised by SCM). The *_note variables and `whose` are short phrases the
    # view composes; a note is empty, or a sentence starting with a space. The quoted
    # text may span lines. Email only: these events never send WhatsApp.
    NotificationEvent(
        code='change_request_raised',
        label='Change request raised, to the Design Head',
        module='Design',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"design change request" or "BOQ change request"',
                     'design change request'),
            Variable('raised_by_name', 'The person who raised it', 'Rahul Verma'),
            Variable('raised_by_capacity', 'PM, Project Coordinator or SCM', 'PM'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('suspended_note',
                     'Empty, or the sentence saying the review in progress is suspended',
                     ' The review in progress is suspended until it is decided.'),
            Variable('fallback_note',
                     'Empty, or the sentence saying it skipped the PM stage and why', ''),
            Variable('reason', 'What the raiser says must change', 'Move the array north.'),
        ),
        in_app_text=('{{ project_id }}: a {{ noun }} is waiting for the Design Head. '
                     '{{ raised_by_name }} ({{ raised_by_capacity }}) raised it on attempt '
                     '{{ attempt_number }}.{{ suspended_note }}{{ fallback_note }} '
                     'Reason: "{{ reason }}"'),
        default_subject='{{ project_id }}: {{ noun }} raised',
        default_body=(
            '{{ project_id }}: a {{ noun }} is waiting for the Design Head. '
            '{{ raised_by_name }} ({{ raised_by_capacity }}) raised it on attempt '
            '{{ attempt_number }}.{{ suspended_note }}{{ fallback_note }}\n\n'
            '**Reason:**\n{{ reason }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The site\'s PM, its active coordinators and every active Design '
                         'Head (not their deputies), except the raiser. Also an SCM request '
                         'on a site with no active PM or coordinator, which skips the PM.'),
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_raised_with_pm',
        label='BOQ change request raised, to the PM',
        module='Design',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"BOQ change request"', 'BOQ change request'),
            Variable('raised_by_name', 'The SCM person who raised it', 'Anil Gupta'),
            Variable('raised_by_capacity', 'SCM', 'SCM'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('reason', 'What the raiser says must change', 'Wrong cable size.'),
        ),
        in_app_text=('{{ project_id }}: a {{ noun }} is waiting for the site\'s PM. '
                     '{{ raised_by_name }} ({{ raised_by_capacity }}) raised it on attempt '
                     '{{ attempt_number }}. Forward it to the Design Head or reject it. '
                     'Reason: "{{ reason }}"'),
        default_subject='{{ project_id }}: {{ noun }} raised',
        default_body=(
            '{{ project_id }}: a {{ noun }} is waiting for the site\'s PM. '
            '{{ raised_by_name }} ({{ raised_by_capacity }}) raised it on attempt '
            '{{ attempt_number }}. Forward it to the Design Head or reject it.\n\n'
            '**Reason:**\n{{ reason }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The site\'s active PM and active coordinators, except the raiser.',
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_forwarded_head',
        label='BOQ change request forwarded, to the Design Head',
        module='Design',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"BOQ change request"', 'BOQ change request'),
            Variable('requester_name', 'The SCM person who raised it', 'Anil Gupta'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('forwarded_by_name', 'The PM or coordinator who forwarded it',
                     'Rahul Verma'),
            Variable('forwarded_by_capacity', 'PM or Project Coordinator', 'PM'),
            Variable('request', 'What the raiser said must change', 'Wrong cable size.'),
        ),
        in_app_text=('{{ project_id }}: a {{ noun }} is waiting for the Design Head. '
                     '{{ requester_name }} (SCM) raised it on attempt {{ attempt_number }} '
                     'and {{ forwarded_by_name }} ({{ forwarded_by_capacity }}) forwarded '
                     'it. Request: "{{ request }}"'),
        default_subject='{{ project_id }}: {{ noun }} forwarded to the Design Head',
        default_body=(
            '{{ project_id }}: a {{ noun }} is waiting for the Design Head. '
            '{{ requester_name }} (SCM) raised it on attempt {{ attempt_number }} and '
            '{{ forwarded_by_name }} ({{ forwarded_by_capacity }}) forwarded it.\n\n'
            '**Request:**\n{{ request }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='Every active Design Head, except the person who forwarded it.',
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_forwarded_requester',
        label='BOQ change request forwarded, to the requester',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"BOQ change request"', 'BOQ change request'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('forwarded_by_name', 'The PM or coordinator who forwarded it',
                     'Rahul Verma'),
            Variable('forwarded_by_capacity', 'PM or Project Coordinator', 'PM'),
            Variable('pm_note', 'The note the PM wrote when forwarding',
                     'Quantities need the Head.'),
        ),
        in_app_text=('{{ project_id }}: {{ forwarded_by_name }} ({{ forwarded_by_capacity }}) '
                     'forwarded your {{ noun }} on attempt {{ attempt_number }} to the '
                     'Design Head. No new attempt has been opened. PM note: '
                     '"{{ pm_note }}"'),
        default_subject='{{ project_id }}: {{ noun }} forwarded to the Design Head',
        default_body=(
            '{{ project_id }}: {{ forwarded_by_name }} ({{ forwarded_by_capacity }}) '
            'forwarded your {{ noun }} on attempt {{ attempt_number }} to the Design Head. '
            'No new attempt has been opened.\n\n'
            '**PM note:**\n{{ pm_note }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The SCM person who raised the request.',
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_pm_rejected',
        label='BOQ change request rejected by the PM',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"BOQ change request"', 'BOQ change request'),
            Variable('rejected_by_name', 'The PM or coordinator who rejected it',
                     'Rahul Verma'),
            Variable('rejected_by_capacity', 'PM or Project Coordinator', 'PM'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('pm_note', 'Why the PM rejected it', 'The BOQ matches the survey.'),
        ),
        in_app_text=('{{ project_id }}: {{ rejected_by_name }} ({{ rejected_by_capacity }}) '
                     'rejected your {{ noun }} on attempt {{ attempt_number }}. It did not '
                     'reach the Design Head and nothing was reopened. PM note: '
                     '"{{ pm_note }}"'),
        default_subject='{{ project_id }}: {{ noun }} rejected by the PM',
        default_body=(
            '{{ project_id }}: {{ rejected_by_name }} ({{ rejected_by_capacity }}) rejected '
            'your {{ noun }} on attempt {{ attempt_number }}. It did not reach the Design '
            'Head and nothing was reopened.\n\n'
            '**PM note:**\n{{ pm_note }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The SCM person who raised the request.',
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_withdrawn',
        label='BOQ change request withdrawn',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"BOQ change request"', 'BOQ change request'),
            Variable('withdrawn_by_name', 'The SCM person who withdrew it', 'Anil Gupta'),
            Variable('whose', '"the BOQ change request they raised", or naming the raiser',
                     'the BOQ change request they raised'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('withdrawal_note', 'Why it was withdrawn', 'Raised in error.'),
        ),
        in_app_text=('{{ project_id }}: {{ withdrawn_by_name }} (SCM) withdrew {{ whose }} '
                     'on attempt {{ attempt_number }}. It is no longer waiting for you. '
                     'Withdrawal note: "{{ withdrawal_note }}"'),
        default_subject='{{ project_id }}: {{ noun }} withdrawn',
        default_body=(
            '{{ project_id }}: {{ withdrawn_by_name }} (SCM) withdrew {{ whose }} on '
            'attempt {{ attempt_number }}. It is no longer waiting for you.\n\n'
            '**Withdrawal note:**\n{{ withdrawal_note }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The site\'s active PM and active coordinators, except the person '
                        'who withdrew it.',
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_accepted',
        label='Change request accepted',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"design change request" or "BOQ change request"',
                     'design change request'),
            Variable('decided_by_name', 'The Design Head, or the deputy, who accepted it',
                     'Meera Iyer'),
            Variable('decided_by_capacity', '"Design Head" or "Design Head\'s deputy"',
                     'Design Head'),
            Variable('whose', '"your design change request", or naming the raiser',
                     'your design change request'),
            Variable('attempt_number', 'The new attempt the acceptance opened', '2'),
            Variable('group_note',
                     'Empty, or the sentence naming the procurement group the site left',
                     ' The site left procurement group "Batch 3".'),
            Variable('request', 'What the raiser said must change', 'Move the array north.'),
        ),
        in_app_text=('{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) '
                     'accepted {{ whose }}. Attempt {{ attempt_number }} is open and the site '
                     'is back with the designer.{{ group_note }} Request: "{{ request }}"'),
        default_subject='{{ project_id }}: {{ noun }} accepted — attempt {{ attempt_number }} '
                        'opened',
        default_body=(
            '{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) accepted '
            '{{ whose }}. Attempt {{ attempt_number }} is open and the site is back with '
            'the designer.{{ group_note }}\n\n'
            '**Request:**\n{{ request }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The raiser, the site\'s PM and active coordinators and the '
                         'designer; and the SCM person who put the site in a draft '
                         'procurement group, if the site left it. Not the decider.'),
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_rejected',
        label='Change request rejected by the Design Head',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"design change request" or "BOQ change request"',
                     'design change request'),
            Variable('decided_by_name', 'The Design Head, or the deputy, who rejected it',
                     'Meera Iyer'),
            Variable('decided_by_capacity', '"Design Head" or "Design Head\'s deputy"',
                     'Design Head'),
            Variable('whose', '"your design change request", or naming the raiser',
                     'your design change request'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('reason', 'Why the Head rejected it', 'The tender fixes the make.'),
        ),
        in_app_text=('{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) '
                     'rejected {{ whose }} on attempt {{ attempt_number }}. The current '
                     'design stands and no new attempt was opened. Reason: "{{ reason }}"'),
        default_subject='{{ project_id }}: {{ noun }} rejected',
        default_body=(
            '{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) rejected '
            '{{ whose }} on attempt {{ attempt_number }}. The current design stands and no '
            'new attempt was opened.\n\n'
            '**Reason:**\n{{ reason }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The raiser; for a BOQ change request, also the site\'s active PM '
                         'and active coordinators. Not the decider.'),
        default_state='active',
    ),
    NotificationEvent(
        code='change_request_corrected',
        label='Change request recorded as corrected in the BOQ',
        module='Design',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('project_id', 'Site ID', 'TND-SITE-007'),
            Variable('noun', '"design change request" or "BOQ change request"',
                     'BOQ change request'),
            Variable('decided_by_name', 'The Design Head, or the deputy', 'Meera Iyer'),
            Variable('decided_by_capacity', '"Design Head" or "Design Head\'s deputy"',
                     'Design Head'),
            Variable('whose', '"your BOQ change request", or naming the raiser',
                     'your BOQ change request'),
            Variable('attempt_number', 'The design attempt it was raised on', '1'),
            Variable('changes', 'The BOQ corrections it cites, up to three, then a count',
                     'CW-001 10 → 12'),
            Variable('note', 'The Head\'s note', 'Raised the module count to 12.'),
        ),
        in_app_text=('{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) '
                     'recorded {{ whose }} on attempt {{ attempt_number }} as corrected in '
                     'the BOQ. No new attempt was opened and the design did not move. '
                     'Changed: {{ changes }}. Note: "{{ note }}"'),
        default_subject='{{ project_id }}: {{ noun }} recorded as corrected in the BOQ',
        default_body=(
            '{{ project_id }}: {{ decided_by_name }} ({{ decided_by_capacity }}) recorded '
            '{{ whose }} on attempt {{ attempt_number }} as corrected in the BOQ. No new '
            'attempt was opened and the design did not move. Changed: {{ changes }}.\n\n'
            '**Note:**\n{{ note }}'
        ),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The raiser and the site\'s active PM and active coordinators. '
                         'Not the decider.'),
        default_state='active',
    ),
])


def get_event(event_code):
    """The registered event, or KeyError — an unknown code is a programmer error."""
    try:
        return NOTIFICATION_EVENTS[event_code]
    except KeyError:
        raise KeyError(f'Unknown notification event {event_code!r}') from None
