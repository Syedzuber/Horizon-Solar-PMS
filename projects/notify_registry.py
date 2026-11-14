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

WHATSAPP (NTF-DCG Session 3a, rulings W1-W2). A deferred event may declare a WhatsApp
leg: a WhatsAppSpec naming the approved Interakt template, its language, its approved
header and body text, and a builder that turns the event's values into
[header, *body]. The template's wording lives in Interakt, not here; the texts are
declared only so the import-time check can verify the counts and Meta's layout rules
against what was approved. A WhatsApp channel starts PAUSED whatever the event's
default_state (default_state_for) and is switched on by Admin only.

This module imports nothing from the app except whatsapp_params, which is pure and
imports nothing itself, so models.py, notify.py and notify_admin.py can all import it
without a cycle.
"""

import re
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

from .whatsapp_params import BODY_LIMIT, HEADER_LIMIT

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

# Interakt/Meta numbered variables in an approved WhatsApp text: {{1}}, {{2}}, ...
WA_VARIABLE_RE = re.compile(r'\{\{\s*(\d+)\s*\}\}')
# Two variables with nothing but whitespace between them, which Meta refuses (Q5).
_WA_ADJACENT_RE = re.compile(r'\{\{\s*\d+\s*\}\}\s*\{\{\s*\d+\s*\}\}')

# The channel whose default is always paused (ruling W2): a new WhatsApp leg sends
# nothing until Admin switches it on, after a real-phone test (notify_admin.set_state).
WHATSAPP = 'whatsapp'


@dataclass(frozen=True)
class Variable:
    name: str
    meaning: str   # one line shown to Admin beside the placeholder
    sample: str    # used by the preview, never by a real send; an EmailTable for a table variable (5c)


@dataclass(frozen=True)
class EmailTable:
    """A table built by the server and passed to notify() as a VALUE (NTF-DCG 5c, E8, N7).

    The one block type an email body can hold besides Admin's paragraphs. Admin's
    wording places it with a paragraph that is only its placeholder, e.g.
    `{{ material_table }}`; notify.render_body_html() then draws an HTML table whose
    every cell is escaped, and render_body_text() prints text(). Admin text itself still
    allows only today's formatting: a table can come from a value, never from wording.
    Anywhere else (inside a sentence, a subject, the in-app line) str() gives text(),
    which the renderers escape like any other value.

    Declared here, beside Variable, and not in notify.py, so an event's samples can hold
    one without this module importing notify (see the module docstring).
    """
    caption: str      # Shown bold above the table; '' for none
    columns: tuple    # Header labels, one per column
    rows: tuple       # One tuple of cell strings per row; '\n' in a cell is a line break
    text_rows: tuple  # One plain-text line per row, for the text part (numbered there)
    footer: tuple = ()  # Plain lines under the table, e.g. an Issue reference

    def __post_init__(self):
        # A builder fault, caught by the first test or preview that builds one.
        if len(self.rows) != len(self.text_rows):
            raise ValueError('EmailTable needs one text row per row')
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError('EmailTable row has the wrong number of cells')

    def text(self):
        """The plain-text form: caption, numbered rows, footer lines. '' when empty, so
        a paragraph holding only an empty table is dropped like an empty value."""
        lines = [self.caption] if self.caption and (self.rows or self.footer) else []
        lines += [f'{n}. {line}' for n, line in enumerate(self.text_rows, start=1)]
        lines += list(self.footer)
        return '\n'.join(lines)

    def __str__(self):
        return self.text()


@dataclass(frozen=True)
class WhatsAppSpec:
    """The WhatsApp leg of an event (ruling W1). Checked at import by _build_registry.

    `params` is called with the event's values (recipient_name and the absolute link
    included) and returns [header, *body] in Interakt's REGISTERED order: params[0] is
    the header value, params[1:] the body values (the convention every approved
    template of ours follows; each has exactly one header variable). Values need not be
    clean: notify() passes them through whatsapp_params.sanitise_params() before the send.
    """
    template: str            # Interakt template name, exactly as approved in the console
    language: str            # Meta language code the template is approved in, e.g. 'en'
    header_text: str         # The approved header, its one variable written {{1}}
    body_text: str           # The approved body, its variables written {{1}}..{{n}}
    header_fixed_chars: int  # Characters of header_text outside its variable; checked against the text
    body_fixed_chars: int    # Characters of body_text outside its variables; checked against the text
    param_count: int         # 1 header value + n body values; the builder must return exactly this many
    params: object           # Callable(values) -> [header, *body]

    def fixed_chars(self, text):
        """`text` without its variables, as Interakt counts the rendered length."""
        return len(WA_VARIABLE_RE.sub('', text))


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
    # NTF-DCG S2 (ruling N9, S1): True sends the external channels (email; WhatsApp from
    # Session 3) from a background thread after commit, so the request never waits on a
    # provider; each send gets a 'pending' log row that moves to sent/failed/skipped.
    # In-app stays synchronous. Opt-in per event: no event shipped before S2 sets it.
    deferred: bool = False
    # NTF-DCG S3a (W1): the WhatsApp leg sent through notify(), or None. Declaring it
    # needs deferred=True and 'whatsapp' in channels (and the reverse); see
    # _whatsapp_errors. Not to be confused with whatsapp_sent_separately, which marks a
    # call site that still sends WhatsApp itself, outside notify().
    whatsapp: WhatsAppSpec = None
    # NTF-DCG 5b (N2): True lets Admin name specific people on the event's page
    # (NotificationEventRecipient) who receive it beside the people recipients_rule
    # resolves; notify_now() merges them into every send. False refuses such rows on add.
    # Opt-in per event: no event shipped in 5b sets it (5c's GRN email is the first).
    named_recipients: bool = False

    def default_state_for(self, channel):
        """The state of `channel` when Admin has never switched it: WhatsApp is always
        paused (ruling W2), every other channel follows default_state. The one rule for
        notify.channel_is_active and the notification screens, so they cannot disagree."""
        if channel == WHATSAPP:
            return 'paused'
        return self.default_state

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


def _wa_text_errors(where, label, text, count, fixed_chars, *, check_ends=True):
    """Faults in one approved WhatsApp text: its variables must be {{1}}..{{count}}, each
    once; it may not hold two side by side (Meta's layout rules, Q5); its declared fixed
    length must match the text. With `check_ends` (the body), it may also not begin or
    end with a variable."""
    errors = []
    numbers = [int(n) for n in WA_VARIABLE_RE.findall(text)]
    if sorted(numbers) != list(range(1, count + 1)):
        errors.append(f'{where}: whatsapp {label} must use {{{{1}}}}..{{{{{count}}}}} '
                      f'once each; it uses {numbers}')
    if len(placeholder_names(text)) != len(numbers):
        errors.append(f'{where}: whatsapp {label} has a {{{{ }}}} that is not a number')
    stripped = text.strip()
    # The begin/end rule is the BODY's only (NTF-DCG 3b, ruling D1). Meta's documented
    # "dangling parameter" refusal is about the body; Interakt/Meta approved dc_created
    # and dc_updated on 4 Nov 2026 with headers ENDING in their variable
    # ("Challan updated – {{1}}"), and no source shows a rule for a header that begins
    # with one either, so the header has neither check. Every body check stays.
    if check_ends and numbers and WA_VARIABLE_RE.match(stripped):
        errors.append(f'{where}: whatsapp {label} may not begin with a variable')
    if check_ends and numbers and re.search(r'\{\{\s*\d+\s*\}\}$', stripped):
        errors.append(f'{where}: whatsapp {label} may not end with a variable')
    if _WA_ADJACENT_RE.search(text):
        errors.append(f'{where}: whatsapp {label} has two variables side by side')
    actual = len(WA_VARIABLE_RE.sub('', text))
    if fixed_chars != actual:
        errors.append(f'{where}: whatsapp {label}_fixed_chars is {fixed_chars}, '
                      f'the text has {actual}')
    return errors


def _whatsapp_errors(event, where):
    """Faults in an event's WhatsApp leg (rulings W1, Q5). Empty when it has none and
    does not list the channel."""
    spec = event.whatsapp
    listed = WHATSAPP in (event.channels or ())
    if spec is None:
        return [f'{where}: lists the whatsapp channel but declares no WhatsAppSpec'] if listed else []
    errors = []
    if not isinstance(spec, WhatsAppSpec):
        return [f'{where}: whatsapp must be a WhatsAppSpec']
    if not event.deferred:
        # W1: WhatsApp is sent only through the deferred path, so the request never
        # waits on Interakt and every send has a pending row.
        errors.append(f'{where}: declares WhatsApp but is not deferred')
    if not listed:
        errors.append(f'{where}: declares WhatsApp but does not list the whatsapp channel')
    if not spec.template or not spec.language:
        errors.append(f'{where}: whatsapp template and language are required')
    if not callable(spec.params):
        errors.append(f'{where}: whatsapp params must be callable')
    if not isinstance(spec.param_count, int) or spec.param_count < 1:
        # The header value is always params[0] (W1), so at least one value.
        return errors + [f'{where}: whatsapp param_count must be at least 1']
    body_count = spec.param_count - 1
    errors += _wa_text_errors(where, 'header', spec.header_text, 1, spec.header_fixed_chars,
                              check_ends=False)
    errors += _wa_text_errors(where, 'body', spec.body_text, body_count, spec.body_fixed_chars)
    # Room for the values once the fixed text is counted: at least one character each,
    # or sanitise_params() could not keep a rendered message under Interakt's limits.
    if HEADER_LIMIT - spec.header_fixed_chars < 1:
        errors.append(f'{where}: whatsapp header text leaves no room for its value')
    if BODY_LIMIT - spec.body_fixed_chars < body_count:
        errors.append(f'{where}: whatsapp body text leaves no room for its values')
    if callable(spec.params):
        try:
            out = spec.params(event.samples())
        except Exception as exc:
            errors.append(f'{where}: whatsapp params builder fails on the samples: '
                          f'{type(exc).__name__}: {exc}')
        else:
            if not isinstance(out, (list, tuple)) or len(out) != spec.param_count:
                got = len(out) if isinstance(out, (list, tuple)) else type(out).__name__
                errors.append(f'{where}: whatsapp params builder returns {got} values, '
                              f'param_count is {spec.param_count}')
    return errors


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
        errors.extend(_whatsapp_errors(event, where))
        registry[event.code] = event
    if errors:
        raise ImproperlyConfigured('; '.join(errors))
    return registry


# --- NTF-DCG 3b: the two approved delivery-challan templates -------------------------
# Copied exactly as Interakt approved them (language en, both with the fixed footer
# "Horizon Renewable Power", which is not a variable and not sent). The dash in each
# header is an en dash, U+2013. The two bodies differ on purpose — "Thanks." / "Thanks",
# and dc_updated's link is followed by a full stop — because that is what was approved.
DC_CREATED_HEADER = 'Material dispatch – {{1}}'
DC_CREATED_BODY = (
    'Hello {{1}},\n\n'
    'Delivery challan {{2}} has been created for site {{3}}.\n\n'
    'Material: {{4}}\n'
    'Expected delivery: {{5}}\n\n'
    'Please check the challan and be ready to receive the material: {{6}}\n\n'
    'Thanks.'
)
DC_UPDATED_HEADER = 'Challan updated – {{1}}'
DC_UPDATED_BODY = (
    'Hello {{1}},\n\n'
    'Delivery challan {{2}} for site {{3}} has been updated by SCM.\n\n'
    'Updated material: {{4}}\n'
    'Expected delivery: {{5}}\n\n'
    'Please refer to the latest challan before receiving the material: {{6}}.\n\n'
    'Thanks'
)


def _dc_whatsapp_params(values):
    """[header, *body] for both challan templates, in Interakt's registered order:
    site code | first name, DC number, site name, material, delivery date, link.
    `link` is already absolute (notify() and test_whatsapp make it so); the rest come
    from delivery_notify.challan_values() and notify()'s recipient_first_name."""
    return [values['site_code'], values['recipient_first_name'], values['dc_number'],
            values['site_name'], values['material_summary'], values['delivery_text'],
            values['link']]


# The variables both challan events declare. recipient_first_name is supplied by
# notify() for each person (it overrides anything passed); it is declared only so the
# W7 test's samples hold one.
_DC_VARIABLES = (
    Variable('site_code', 'The site code (the project ID)', 'UKRU001'),
    Variable('site_name', 'The site name; for a site without one, the customer (and '
             'city, on a tender site)', 'Rashtrapati Udyan-Dehradun'),
    Variable('dc_number', 'The challan number, as printed on the vendor document', '004'),
    Variable('material_summary', 'Up to five lines "description qty unit", then '
             '"+N more items" (never a rate or amount)',
             '1C x 4 Sqmm DC solar cable 2000 Mtr; MC4 Connector 200 Pair'),
    Variable('delivery_text', 'The expected delivery date, or "Not given (dispatched '
             '<DC date>)"', '08 Nov 2026'),
    Variable('recipient_first_name', 'The recipient\'s first name, else their username '
             '(filled in for each person)', 'Priya'),
)


# --- NTF-DCG 5c: the goods-receipt email's variables ---------------------------------
# Built by grn_notify.grn_values(). The two tables are EmailTable values (E8); their
# samples are tables too, so Admin's preview and test send show one (5c-Q7).
_GRN_MATERIAL_SAMPLE = EmailTable(
    caption='Material',
    columns=('Item', 'Received', 'Damage / notes'),
    rows=(
        ('1C x 4 Sqmm DC solar cable\nMtr · DC qty 2000', '— → 1500 (+1500)',
         'Good\nTwo drums; third to follow'),
        ('MC4 Connector\nPair · DC qty 250', '200 → 250 (+50)',
         'Damaged 0 → 2 (+2)\nDamaged'),
    ),
    text_rows=(
        '1C x 4 Sqmm DC solar cable — Unit: Mtr · DC qty: 2000 · Received: — → 1500 '
        '(+1500) · Condition: Good · Notes: Two drums; third to follow',
        'MC4 Connector — Unit: Pair · DC qty: 250 · Received: 200 → 250 (+50) · '
        'Damaged: 0 → 2 (+2) · Condition: Damaged',
    ),
)
_GRN_DAMAGE_SAMPLE = EmailTable(
    caption='Damage',
    columns=('Item', 'Damaged', 'Condition / notes'),
    rows=(('Panel 540Wp\nNos · DC qty 40', '0 → 2 (+2)', 'Damaged\nTwo cracked across the glass'),),
    text_rows=('Panel 540Wp — Unit: Nos · Damaged: 0 → 2 (+2) · Condition: Damaged · '
               'Notes: Two cracked across the glass',),
    footer=('Issue #45: Damaged delivery — DC 004', 'Damage photos linked to this receipt: 2'),
)
_GRN_VARIABLES = (
    Variable('subject_prefix', '"Correction: " for an SCM correction, else empty', ''),
    Variable('grn_number', 'The receipt number', 'GRN-000123'),
    Variable('receipt_kind', '"Receipt" or "Correction (by SCM)", with the on-behalf '
             'reason when SCM recorded a receipt the site team had not', 'Receipt'),
    Variable('recorded_by_name', 'Who recorded the GRN', 'Arfan Khan'),
    Variable('receipt_date', 'The receipt date, e.g. "05 Nov 2026"', '05 Nov 2026'),
    Variable('site_code', 'The site code (the project ID)', 'UKRU001'),
    Variable('site_name', 'The site name; for a site without one, the customer (and '
             'city, on a tender site)', 'Rashtrapati Udyan-Dehradun'),
    Variable('dc_number', 'The challan number, as printed on the vendor document', '004'),
    Variable('dc_date', 'The challan date, e.g. "04 Nov 2026"', '04 Nov 2026'),
    Variable('vendor_name', 'The challan\'s vendor, or "Not recorded"', 'Polycab India Ltd'),
    Variable('po_reference', 'The challan\'s PO/PI number, or "not linked"', 'not linked'),
    Variable('status_after', 'The challan status after this receipt', 'Partially Received'),
    Variable('material_table', 'A table of the receipt\'s lines: item, unit and DC qty; '
             'received before → after; damage, condition and notes. Put it on a line '
             'of its own', _GRN_MATERIAL_SAMPLE),
    Variable('damage_section', 'The damaged lines with their Issue and photo count, '
             'or nothing when no line is damaged. Put it on a line of its own',
             _GRN_DAMAGE_SAMPLE),
    Variable('documents_summary', 'Documents attached at this receipt, counted by type, '
             'or "No documents attached"', 'Signed DC copy: 1 · Delivery photo: 2'),
)


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
                         'The project managers get a separate in-app note only. A '
                         'damaged-delivery issue raised from a GRN is sent the same way: '
                         'SCM are not sent it, because the "Goods receipt recorded" email '
                         'already shows them the damage.'),
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
    # FIN-M-R9: renamed "Payment received" and "initiate collection" dropped — the alert
    # fires when the money has been recorded (task Done, or Finance's receipt via the
    # FIN-M-B9a chokepoint), so there is nothing left to collect. The CODE is unchanged:
    # log rows, setting rows, Admin template versions and tests key on it.
    NotificationEvent(
        code='payment_milestone_reached',
        label='Payment received',
        module='Payments',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('task_name', 'The payment confirmation task that was completed',
                     'Pre Dispatch Payment Confirmation'),
            Variable('recorded_by_name', 'The person who recorded the payment',
                     'Ravi Verma'),
        ),
        in_app_text=('Payment received for project {{ project_id }} ({{ customer_name }}): '
                     '"{{ task_name }}", recorded by {{ recorded_by_name }}.'),
        default_subject='Payment received — {{ customer_name }}',
        default_body=(
            'Payment received for project {{ project_id }} ({{ customer_name }}).\n\n'
            '"{{ task_name }}" was recorded by {{ recorded_by_name }}.'
        ),
        button_label='View in Horizon Solar PMS',
        recipients_rule=('BD and CEO users and the project managers (PM and coordinators), '
                         'except the person who recorded the payment. Finance users are '
                         'included only when the payment was recorded by someone who is '
                         'not Finance; this interim rule ends when PMs and coordinators '
                         'can no longer record payments (FIN-M-R10).'),
        default_state='active',
        whatsapp_sent_separately=True,
    ),

    # --- FIN-M-B4: an M2/M3 invoice has fallen due (R8) -----------------------------
    # Fired only from views._apply_task_status_change, after commit, when an R8 trigger
    # task (utils.INVOICE_DUE_TRIGGERS) is marked Done while its milestone is Pending.
    # Email starts PAUSED (Admin switches it on after a test send); in-app is never
    # paused, so it is live from the deploy (ruling (A)). Deferred, like every external
    # send added since S2. No WhatsApp: there is no approved Interakt template. The
    # agreed amount is in the email only; every recipient is Finance.
    NotificationEvent(
        code='invoice_due',
        label='Invoice due',
        module='Payments',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
            Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
            Variable('milestone', 'The payment milestone now due: M2 or M3', 'M2'),
            Variable('trigger_task', 'The task whose completion made the invoice due',
                     'PO for B & C Class Items'),
            Variable('completed_by_name', 'The person who marked that task Done',
                     'Ravi Verma'),
            Variable('completed_on', 'The day it was marked Done, e.g. "13 Nov 2026"',
                     '13 Nov 2026'),
            Variable('agreed_amount', 'The milestone\'s agreed amount in rupees, or '
                     '"amount not set"', '₹1,35,000'),
        ),
        in_app_text=('{{ milestone }} invoice is now due for project {{ project_id }} '
                     '({{ customer_name }}): "{{ trigger_task }}" was completed by '
                     '{{ completed_by_name }}.'),
        default_subject='Invoice due — {{ customer_name }} ({{ milestone }})',
        default_body=(
            'Hello {{ recipient_name }},\n\n'
            'The {{ milestone }} invoice for project {{ project_id }} ({{ customer_name }}) '
            'is now due.\n\n'
            '"{{ trigger_task }}" was marked Done by {{ completed_by_name }} on '
            '{{ completed_on }}.\n\n'
            '**Agreed amount:** {{ agreed_amount }}\n\n'
            'Record the invoice on the project\'s payment milestone card.'
        ),
        button_label='Open the milestone card',
        recipients_rule=('Every active Finance user, except the person who completed the '
                         'task. Sent when "PO for B & C Class Items" (M2) or "Customer '
                         'Handover" (M3) is marked Done on a Residential site that is not '
                         'deleted, Draft or Cancelled, while that milestone is Pending. '
                         'Once per site and milestone: completing the task again after a '
                         'reopen sends nothing. Never sent for a task marked Not '
                         'Applicable.'),
        default_state='paused',
        deferred=True,
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

    # --- T3c: approvals and contractor bills -------------------------------------------
    # Wording pinned by tests_notify_migration_c. approval_notices.py sends these from its
    # after-commit callbacks with notify_now(); its in-app-only notices (superseded,
    # replaced, kept, proxy, reassigned away) stay outside the registry. One event per
    # wording (ruling Q1): the two sent to a Site Engineer (approval_work_to_confirm,
    # approval_withdrawn_work) carry the WORK summary only and declare no variable that
    # can hold a bill amount (D-A53, M4). Every other approval event, approval_work_not_done
    # to the raiser included, carries summary_line, which on a bill names its amount.
    # `asked_by` is the sentence start saying who asks and why now (ruling Q2). note_clause
    # is empty or a phrase starting with a space. Email only: none of these sends WhatsApp.
    NotificationEvent(
        code='approval_step_activated',
        label='Approval needed, to the approver',
        module='Approvals',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('asked_by', 'Who asks, and why now: "<name> asks you", or the resubmit, '
                     'reassign or earlier-approval form', 'Anil Gupta asks you'),
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('party_label', 'The capacity asked: PM or Design Head', 'PM'),
            Variable('summary_line',
                     'Empty, or "Material: <first line>, +N more", or for a contractor bill '
                     '"Bill: <contractor> · <site> · bill <number> · <amount>"',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('{{ asked_by }} to approve "{{ approval_title }}" ({{ kind_label }}) as '
                     'the {{ party_label }}. Open it to approve, request changes or reject.'),
        default_subject='Approval needed: {{ approval_title }}',
        default_body=(
            '{{ asked_by }} to approve "{{ approval_title }}" ({{ kind_label }}) as the '
            '{{ party_label }}. Open it to approve, request changes or reject.\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule=('Each assignee whose step this action activated (create, resubmit, '
                         'reassign, or an earlier sequence approving). Not the actor.'),
        default_state='active',
    ),
    NotificationEvent(
        code='approval_work_to_confirm',
        label='Work to confirm, to the Site Engineer',
        module='Approvals',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('asked_by', 'Who asks, and why now: "<name> asks you", or the resubmit '
                     'or reassign form', 'Anil Gupta asks you'),
            Variable('approval_title', 'Title of the contractor bill, as the raiser typed it',
                     'Civil works bill'),
            Variable('kind_label', 'Kind of request', 'Contractor bill'),
            Variable('work_summary', 'The contractor, the site and the first task, then a '
                     'count. Never the amount or bill number',
                     'Civil Co · HRP-RES-2026-001 · Foundation, +1 more'),
        ),
        in_app_text=('{{ asked_by }} to confirm the work on "{{ approval_title }}" '
                     '({{ kind_label }}). Open it to confirm the work done, or say it is not '
                     'done.'),
        default_subject='Work to confirm: {{ approval_title }}',
        default_body=(
            '{{ asked_by }} to confirm the work on "{{ approval_title }}" ({{ kind_label }}). '
            'Open it to confirm the work done, or say it is not done.\n\n'
            'Work: {{ work_summary }}'
        ),
        button_label='Open the approval',
        recipients_rule=('The contractor bill\'s Site Engineer, when their step is activated '
                         '(create, resubmit, reassign). Not the actor.'),
        default_state='active',
    ),
    NotificationEvent(
        code='approval_step_activated_deputy',
        label='Approval needs a Design Head decision, to the Head\'s deputy',
        module='Approvals',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('head_name', 'The Design Head the step is with', 'Meera Iyer'),
            Variable('summary_line', 'Empty, or "Material: <first line>, +N more"',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('"{{ approval_title }}" ({{ kind_label }}) needs a Design Head decision. '
                     'It is with {{ head_name }}; as their named deputy you may decide it.'),
        default_subject='Approval needed: {{ approval_title }}',
        default_body=(
            '"{{ approval_title }}" ({{ kind_label }}) needs a Design Head decision. It is '
            'with {{ head_name }}; as their named deputy you may decide it.\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule=('The named deputy of the Design Head whose step was activated, '
                         'unless the deputy raised the request or is also an assignee.'),
        default_state='active',
    ),
    NotificationEvent(
        code='approval_approved',
        label='Approval request approved, to the raiser',
        module='Approvals',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('decided_by_name', 'Who gave the last approval', 'Rahul Verma'),
            Variable('party_label', 'The capacity they approved in', 'PM'),
            Variable('note_clause', 'Empty, or \' Note: "<their note>"\'',
                     ' Note: "Go with Waaree."'),
            Variable('summary_line', 'Empty, or the Material or Bill line',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('"{{ approval_title }}" ({{ kind_label }}) is approved. '
                     '{{ decided_by_name }} gave the last approval, as the {{ party_label }}.'
                     '{{ note_clause }}'),
        default_subject='Approved: {{ approval_title }}',
        default_body=(
            '"{{ approval_title }}" ({{ kind_label }}) is approved. {{ decided_by_name }} gave '
            'the last approval, as the {{ party_label }}.{{ note_clause }}\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule='The person who raised the request, unless they recorded the decision.',
        default_state='active',
    ),
    NotificationEvent(
        code='approval_changes_requested',
        label='Approval request: changes requested, to the raiser',
        module='Approvals',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('decided_by_name', 'Who requested changes', 'Rahul Verma'),
            Variable('party_label', 'The capacity they decided in', 'PM'),
            Variable('note_clause', 'Empty, or \' Note: "<their note>"\'',
                     ' Note: "Quote the 550 Wp variant."'),
            Variable('summary_line', 'Empty, or the Material or Bill line',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('{{ decided_by_name }} requested changes to "{{ approval_title }}" '
                     '({{ kind_label }}) as the {{ party_label }}. Revise and resubmit it, or '
                     'withdraw it.{{ note_clause }}'),
        default_subject='Changes requested: {{ approval_title }}',
        default_body=(
            '{{ decided_by_name }} requested changes to "{{ approval_title }}" '
            '({{ kind_label }}) as the {{ party_label }}. Revise and resubmit it, or withdraw '
            'it.{{ note_clause }}\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule='The person who raised the request, unless they recorded the decision.',
        default_state='active',
    ),
    NotificationEvent(
        code='approval_work_not_done',
        label='Contractor bill: work not done, to the raiser',
        module='Approvals',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('approval_title', 'Title of the contractor bill, as the raiser typed it',
                     'Civil works bill'),
            Variable('kind_label', 'Kind of request', 'Contractor bill'),
            Variable('decided_by_name', 'The Site Engineer', 'Suresh Kumar'),
            Variable('party_label', 'The capacity they decided in', 'Site Engineer'),
            Variable('note_clause', 'Empty, or \' Note: "<their note>"\'',
                     ' Note: "Block B not finished."'),
            Variable('summary_line', 'Empty, or "Bill: <contractor> · <site> · bill <number> '
                     '· <amount>"',
                     'Bill: Civil Co · HRP-RES-2026-001 · bill CB-7 · ₹12,500.00'),
        ),
        in_app_text=('{{ decided_by_name }} said the work on "{{ approval_title }}" '
                     '({{ kind_label }}) is not done, as the {{ party_label }}. Revise and '
                     'resubmit it, or withdraw it.{{ note_clause }}'),
        default_subject='Changes requested: {{ approval_title }}',
        default_body=(
            '{{ decided_by_name }} said the work on "{{ approval_title }}" ({{ kind_label }}) '
            'is not done, as the {{ party_label }}. Revise and resubmit it, or withdraw it.'
            '{{ note_clause }}\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule='The person who raised the bill, unless they recorded the decision.',
        default_state='active',
    ),
    NotificationEvent(
        code='approval_rejected',
        label='Approval request rejected, to the raiser',
        module='Approvals',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('decided_by_name', 'Who rejected it', 'Rahul Verma'),
            Variable('party_label', 'The capacity they decided in', 'PM'),
            Variable('note_clause', 'Empty, or \' Note: "<their note>"\'',
                     ' Note: "Vendor blacklisted."'),
            Variable('summary_line', 'Empty, or the Material or Bill line',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('{{ decided_by_name }} rejected "{{ approval_title }}" ({{ kind_label }}) '
                     'as the {{ party_label }}.{{ note_clause }}'),
        default_subject='Rejected: {{ approval_title }}',
        default_body=(
            '{{ decided_by_name }} rejected "{{ approval_title }}" ({{ kind_label }}) as the '
            '{{ party_label }}.{{ note_clause }}\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule='The person who raised the request, unless they recorded the decision.',
        default_state='active',
    ),
    NotificationEvent(
        code='approval_withdrawn',
        label='Approval request withdrawn, to the approvers',
        module='Approvals',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('withdrawn_by_name', 'Who withdrew it', 'Anil Gupta'),
            Variable('approval_title', 'Title of the approval request', 'Module make'),
            Variable('kind_label', 'Kind of request', 'Material — before order'),
            Variable('reason', 'Why it was withdrawn', 'Vendor dropped.'),
            Variable('summary_line', 'Empty, or the Material or Bill line',
                     'Material: 120 Nos — Solar module 545 Wp, +2 more'),
        ),
        in_app_text=('{{ withdrawn_by_name }} withdrew "{{ approval_title }}" ({{ kind_label }}). '
                     'You no longer need to decide it. Reason: "{{ reason }}"'),
        default_subject='Withdrawn: {{ approval_title }}',
        default_body=(
            '{{ withdrawn_by_name }} withdrew "{{ approval_title }}" ({{ kind_label }}). You '
            'no longer need to decide it. Reason: "{{ reason }}"\n\n'
            '{{ summary_line }}'
        ),
        button_label='Open the approval',
        recipients_rule=('Each assignee whose activated, pending step the withdrawal '
                         'superseded, except a contractor bill\'s Site Engineer. Not the actor.'),
        default_state='active',
    ),
    NotificationEvent(
        code='approval_withdrawn_work',
        label='Contractor bill withdrawn, to the Site Engineer',
        module='Approvals',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('withdrawn_by_name', 'Who withdrew it', 'Anil Gupta'),
            Variable('approval_title', 'Title of the contractor bill, as the raiser typed it',
                     'Civil works bill'),
            Variable('kind_label', 'Kind of request', 'Contractor bill'),
            Variable('reason', 'Why it was withdrawn', 'Bill resent.'),
            Variable('work_summary', 'The contractor, the site and the first task, then a '
                     'count. Never the amount or bill number',
                     'Civil Co · HRP-RES-2026-001 · Foundation, +1 more'),
        ),
        in_app_text=('{{ withdrawn_by_name }} withdrew "{{ approval_title }}" ({{ kind_label }}). '
                     'You no longer need to decide it. Reason: "{{ reason }}"'),
        default_subject='Withdrawn: {{ approval_title }}',
        default_body=(
            '{{ withdrawn_by_name }} withdrew "{{ approval_title }}" ({{ kind_label }}). You '
            'no longer need to decide it. Reason: "{{ reason }}"\n\n'
            'Work: {{ work_summary }}'
        ),
        button_label='Open the approval',
        recipients_rule=('The contractor bill\'s Site Engineer, if their step was activated '
                         'and still pending. Not the actor.'),
        default_state='active',
    ),

    # --- T3c: COD, punch points, QA/QC, PM handover ------------------------------------
    # Wording pinned by tests_notify_migration_c. Before T3c each of these emailed its
    # one-sentence in-app text as both subject and text body, with no link; the subject
    # is kept whole (M2) and listed in SECONDARY_FINDINGS.md for Admin to shorten. Each
    # call site already runs on commit and sends with notify_now(). Email only.
    NotificationEvent(
        code='cod_recorded',
        label='COD recorded',
        module='Closeout',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
            Variable('cod_date', 'The COD date', '14 Oct 2026'),
            Variable('evidence', 'The evidence recorded', 'DISCOM letter'),
            Variable('recorded_by_name', 'Who recorded it', 'Rahul Verma'),
        ),
        in_app_text=('COD recorded for {{ site_label }}: {{ cod_date }}, evidence '
                     '{{ evidence }}. Recorded by {{ recorded_by_name }}.'),
        default_subject=('COD recorded for {{ site_label }}: {{ cod_date }}, evidence '
                         '{{ evidence }}. Recorded by {{ recorded_by_name }}.'),
        default_body=('COD recorded for {{ site_label }}: {{ cod_date }}, evidence '
                      '{{ evidence }}. Recorded by {{ recorded_by_name }}.'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The site\'s PM, its active coordinators and every active CEO, '
                         'except the person who recorded it.'),
        default_state='active',
    ),
    NotificationEvent(
        code='cod_withdrawn',
        label='COD withdrawn',
        module='Closeout',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
            Variable('withdrawn_by_name', 'Who withdrew it', 'Rahul Verma'),
            Variable('reason', 'Why it was withdrawn', 'The letter was for the wrong meter.'),
        ),
        in_app_text='COD withdrawn for {{ site_label }} by {{ withdrawn_by_name }}: {{ reason }}',
        default_subject=('COD withdrawn for {{ site_label }} by {{ withdrawn_by_name }}: '
                         '{{ reason }}'),
        default_body='COD withdrawn for {{ site_label }} by {{ withdrawn_by_name }}: {{ reason }}',
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The site\'s PM, its active coordinators and every active CEO, '
                         'except the person who withdrew it.'),
        default_state='active',
    ),
    NotificationEvent(
        code='punch_point_raised_by_visit',
        label='Punch points raised by a QA/QC visit, to the task\'s assignee',
        module='Closeout',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('visit_date', 'The visit\'s date', '14 Oct 2026'),
            Variable('points', 'How many it raised on the task: "1 punch point", '
                     '"3 punch points"', '2 punch points'),
            Variable('task_name', 'The task', 'Module Installation'),
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
        ),
        in_app_text=('QA/QC visit {{ visit_date }} raised {{ points }} on your task '
                     '\'{{ task_name }}\' at {{ site_label }}. Rectify each with a remark and '
                     'a photo.'),
        default_subject=('QA/QC visit {{ visit_date }} raised {{ points }} on your task '
                         '\'{{ task_name }}\' at {{ site_label }}. Rectify each with a remark '
                         'and a photo.'),
        default_body=('QA/QC visit {{ visit_date }} raised {{ points }} on your task '
                      '\'{{ task_name }}\' at {{ site_label }}. Rectify each with a remark and '
                      'a photo.'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The assignee of each task the visit raised points on, once per '
                         'task. An unassigned task tells nobody.'),
        default_state='active',
    ),
    NotificationEvent(
        code='punch_point_rectified',
        label='Punch point rectified, awaiting re-verification',
        module='Closeout',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('task_name', 'The task', 'Module Installation'),
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
        ),
        in_app_text=('Punch point on \'{{ task_name }}\' at {{ site_label }} was marked '
                     'rectified and is awaiting re-verification.'),
        default_subject=('Punch point on \'{{ task_name }}\' at {{ site_label }} was marked '
                         'rectified and is awaiting re-verification.'),
        default_body=('Punch point on \'{{ task_name }}\' at {{ site_label }} was marked '
                      'rectified and is awaiting re-verification.'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The site\'s QA/QC engineer; when none can act, the PM and active '
                         'coordinators. Not the person who rectified it.'),
        default_state='active',
    ),
    NotificationEvent(
        code='punch_point_reverify_rejected',
        label='Punch point rectification not accepted, to the task\'s assignee',
        module='Closeout',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('task_name', 'The task', 'Module Installation'),
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
            Variable('reason', 'Why it was not accepted, cut so the in-app text stays within '
                     '500 characters', 'Clamps still loose on string 3.'),
        ),
        in_app_text=('The rectification of a punch point on \'{{ task_name }}\' at '
                     '{{ site_label }} was not accepted: {{ reason }}'),
        default_subject=('The rectification of a punch point on \'{{ task_name }}\' at '
                         '{{ site_label }} was not accepted: {{ reason }}'),
        default_body=('The rectification of a punch point on \'{{ task_name }}\' at '
                      '{{ site_label }} was not accepted: {{ reason }}'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The task\'s assignee, unless they re-verified it themselves.',
        default_state='active',
    ),
    NotificationEvent(
        code='site_qaqc_assigned',
        label='QA/QC engineer assigned, to the engineer',
        module='Closeout',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
        ),
        in_app_text=('You are now the QA/QC engineer for {{ site_label }}. You can view the '
                     'site and approve or reject its submitted tasks.'),
        default_subject=('You are now the QA/QC engineer for {{ site_label }}. You can view '
                         'the site and approve or reject its submitted tasks.'),
        default_body=('You are now the QA/QC engineer for {{ site_label }}. You can view the '
                      'site and approve or reject its submitted tasks.'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The engineer named, unless they named themselves.',
        default_state='active',
    ),
    NotificationEvent(
        code='site_qaqc_removed',
        label='QA/QC engineer removed, to the engineer',
        module='Closeout',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
        ),
        in_app_text='You are no longer the QA/QC engineer for {{ site_label }}.',
        default_subject='You are no longer the QA/QC engineer for {{ site_label }}.',
        default_body='You are no longer the QA/QC engineer for {{ site_label }}.',
        button_label='Open in Horizon Solar PMS',
        recipients_rule=('The outgoing engineer, when replaced or ended, unless they made the '
                         'change themselves.'),
        default_state='active',
    ),
    NotificationEvent(
        code='qaqc_visit_submitted',
        label='QA/QC visit submitted, to the site\'s managers',
        module='Closeout',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('site_label', 'The site code, and the customer name when there is one',
                     'MB0001 (Ram Prasad)'),
            Variable('engineer_name', 'The QA/QC engineer who submitted it', 'Suresh Kumar'),
            Variable('points_checked', 'How many points were checked: "12 points"',
                     '12 points'),
            Variable('no_count', 'How many were answered No', '2'),
            Variable('points_raised', 'How many punch points it raised: "2 punch points"',
                     '2 punch points'),
        ),
        in_app_text=('QA/QC visit submitted for {{ site_label }} by {{ engineer_name }}: '
                     '{{ points_checked }} checked, {{ no_count }} No, {{ points_raised }} '
                     'raised.'),
        default_subject=('QA/QC visit submitted for {{ site_label }} by {{ engineer_name }}: '
                         '{{ points_checked }} checked, {{ no_count }} No, {{ points_raised }} '
                         'raised.'),
        default_body=('QA/QC visit submitted for {{ site_label }} by {{ engineer_name }}: '
                      '{{ points_checked }} checked, {{ no_count }} No, {{ points_raised }} '
                      'raised.'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The site\'s PM and active coordinators, never the engineer.',
        default_state='active',
    ),
    NotificationEvent(
        code='pm_handover_incoming',
        label='Sites handed over, to the incoming PM',
        module='Sites',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('actor_name', 'The Admin who made the handover', 'Asha Rao'),
            Variable('sites', 'The site, or "N sites in <tender> (codes)"',
                     '3 sites in MPUVNL (MB0001, MB0002, MB0003)'),
            Variable('outgoing_pm_name', 'The PM the sites came from', 'Rahul Verma'),
            Variable('moved', 'What moved: "N open tasks and N bill approval steps"',
                     '6 open tasks and 2 bill approval steps'),
            Variable('reason', 'Why the sites were handed over', 'Rahul moves to IPGCL.'),
        ),
        in_app_text=('{{ actor_name }} handed over {{ sites }} to you from '
                     '{{ outgoing_pm_name }}: {{ moved }} moved to you. Reason: {{ reason }}'),
        default_subject=('{{ actor_name }} handed over {{ sites }} to you from '
                         '{{ outgoing_pm_name }}: {{ moved }} moved to you. Reason: '
                         '{{ reason }}'),
        default_body=('{{ actor_name }} handed over {{ sites }} to you from '
                      '{{ outgoing_pm_name }}: {{ moved }} moved to you. Reason: {{ reason }}'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The incoming PM, once per handover (never once per site).',
        default_state='active',
    ),
    NotificationEvent(
        code='pm_handover_outgoing',
        label='Sites handed over, to the outgoing PM',
        module='Sites',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('actor_name', 'The Admin who made the handover', 'Asha Rao'),
            Variable('sites', 'The site, or "N sites in <tender> (codes)"',
                     '3 sites in MPUVNL (MB0001, MB0002, MB0003)'),
            Variable('incoming_pm_name', 'The PM the sites went to', 'Priya Nair'),
            Variable('moved', 'What moved: "N open tasks and N bill approval steps"',
                     '6 open tasks and 2 bill approval steps'),
            Variable('material_note', 'Empty, or the sentence saying how many material '
                     'approval steps still name them and that SCM will reassign them',
                     ' 2 material approval steps still name you; SCM will reassign them.'),
            Variable('reason', 'Why the sites were handed over', 'Rahul moves to IPGCL.'),
        ),
        in_app_text=('{{ actor_name }} handed over {{ sites }} from you to '
                     '{{ incoming_pm_name }}: {{ moved }} moved to {{ incoming_pm_name }}.'
                     '{{ material_note }} Reason: {{ reason }}'),
        default_subject=('{{ actor_name }} handed over {{ sites }} from you to '
                         '{{ incoming_pm_name }}: {{ moved }} moved to {{ incoming_pm_name }}.'
                         '{{ material_note }} Reason: {{ reason }}'),
        default_body=('{{ actor_name }} handed over {{ sites }} from you to '
                      '{{ incoming_pm_name }}: {{ moved }} moved to {{ incoming_pm_name }}.'
                      '{{ material_note }} Reason: {{ reason }}'),
        button_label='Open in Horizon Solar PMS',
        recipients_rule='The outgoing PM, once per handover (never once per site).',
        default_state='active',
    ),

    # --- ATT-B4: contractor attendance not logged -------------------------------------
    # Sent by send_attendance_alerts, not from a view. Starts PAUSED (ruling 5): Admin
    # switches email on after a test send. Only 'email' is declared because the command
    # sends the WhatsApp leg itself, gated by settings.ATTENDANCE_ALERT_WHATSAPP_ENABLED,
    # as send_morning_task_report does; an Admin WhatsApp switch here would control
    # nothing (SECONDARY_FINDINGS, ATT-B4).
    NotificationEvent(
        code='attendance_missing',
        label='Contractor attendance not logged',
        module='Attendance',
        tier=TIER_ACT,
        channels=('email',),
        variables=(
            Variable('date', 'The day checked, e.g. "31 Oct"', '31 Oct'),
            Variable('cutoff', 'The time the check ran, e.g. "10:30 AM"', '10:30 AM'),
            Variable('sites', 'The site codes, comma-separated: at most 15, then "and N more"',
                     'MB0001, MB0002 and 3 more'),
            Variable('site_lines', 'One line per site: its code and a link to its attendance '
                     'page (at most 15, then "and N more")',
                     'MB0001 – https://horizon-solar-pms-production.up.railway.app/projects/'
                     'MB0001/attendance/\nMB0002 – https://horizon-solar-pms-production.up.'
                     'railway.app/projects/MB0002/attendance/'),
        ),
        in_app_text=('By {{ cutoff }} today no contractor attendance has been logged on these '
                     'sites with installation work in progress: {{ sites }}.'),
        default_subject='Attendance not logged by {{ cutoff }} – {{ date }}',
        default_body=(
            'Hello {{ recipient_name }},\n\n'
            'By {{ cutoff }} today no contractor attendance has been logged on these sites '
            'with installation work in progress:\n\n'
            '{{ site_lines }}\n\n'
            'Please follow up with the site engineers and get the crews logged in Horizon PMS.'
        ),
        button_label='Open your dashboard',
        recipients_rule=('Each site\'s assigned PM (coordinators are not included), one '
                         'message listing all their sites; and every active SCM user, one '
                         'message listing every site. Sent once a day by '
                         'send_attendance_alerts. Its WhatsApp message is sent by that '
                         'command and is not paused here.'),
        default_state='paused',
        whatsapp_sent_separately=True,
    ),

    # --- NTF-DCG 3b: delivery challan created / updated -------------------------------
    # In-app and WhatsApp, no email (ruling D2). Deferred, as every WhatsApp event must
    # be. The WhatsApp channel starts paused whatever default_state says
    # (default_state_for); 'active' here is the in-app default. Fired only by
    # delivery_notify, from the create view and the challan edit save.
    NotificationEvent(
        code='dc_created',
        label='Delivery challan created',
        module='Delivery',
        tier=TIER_ACT,
        channels=('whatsapp',),
        variables=_DC_VARIABLES,
        in_app_text=('Delivery challan {{ dc_number }} created for {{ site_code }} — '
                     'expected delivery: {{ delivery_text }}.'),
        default_subject='',
        default_body='',
        button_label='',
        recipients_rule=('Active Site Engineers holding at least one open task (not Done, '
                         'not Not Applicable) on the site, except its QA/QC engineer; if '
                         'none, the site\'s assigned PM. Never the person who created the '
                         'challan.'),
        default_state='active',
        deferred=True,
        whatsapp=WhatsAppSpec(
            template='dc_created', language='en',
            header_text=DC_CREATED_HEADER, body_text=DC_CREATED_BODY,
            header_fixed_chars=20, body_fixed_chars=160, param_count=7,
            params=_dc_whatsapp_params),
    ),
    NotificationEvent(
        code='dc_updated',
        label='Delivery challan updated',
        module='Delivery',
        tier=TIER_ACT,
        channels=('whatsapp',),
        variables=_DC_VARIABLES,
        in_app_text=('Delivery challan {{ dc_number }} for {{ site_code }} updated by SCM — '
                     'expected delivery: {{ delivery_text }}.'),
        default_subject='',
        default_body='',
        button_label='',
        recipients_rule=('As "Delivery challan created". Sent only when an edit changes a '
                         'line or the expected delivery date, and never once the challan '
                         'is Received or Rejected.'),
        default_state='active',
        deferred=True,
        whatsapp=WhatsAppSpec(
            template='dc_updated', language='en',
            header_text=DC_UPDATED_HEADER, body_text=DC_UPDATED_BODY,
            header_fixed_chars=18, body_fixed_chars=178, param_count=7,
            params=_dc_whatsapp_params),
    ),

    # --- NTF-DCG 5c: a goods receipt recorded -----------------------------------------
    # Email (rulings E1-E9, N7) to SCM, the site's PM and the named Finance recipients:
    # the record a vendor payment is reconciled against. Deferred, like every external
    # send added since S2. The email starts PAUSED (Admin switches it on after a test of
    # the live version); in-app is never paused, so it is live from the push (5c-Q6).
    # Fired only by grn_notify, from goods_receipts.write_goods_receipt(), after commit.
    # No variable carries a rate, price or amount (tests_grn_notify pins it).
    NotificationEvent(
        code='grn_recorded',
        label='Goods receipt recorded',
        module='Delivery',
        tier=TIER_KNOW,
        channels=('email',),
        variables=_GRN_VARIABLES,
        in_app_text=('{{ grn_number }} recorded on DC {{ dc_number }} ({{ site_code }}) — '
                     '{{ status_after }}.'),
        default_subject=('{{ subject_prefix }}{{ grn_number }} · {{ site_code }} · '
                         'DC {{ dc_number }} · {{ status_after }}'),
        default_body=(
            'Hello {{ recipient_name }},\n\n'
            '{{ grn_number }} has been recorded on delivery challan {{ dc_number }} for '
            '{{ site_code }}.\n\n'
            '**GRN:** {{ grn_number }} — {{ receipt_kind }}\n'
            '**Recorded by:** {{ recorded_by_name }} on {{ receipt_date }}\n'
            '**Site:** {{ site_code }} — {{ site_name }}\n'
            '**Challan:** DC {{ dc_number }} dated {{ dc_date }}\n'
            '**Vendor:** {{ vendor_name }}\n'
            '**PO/PI:** {{ po_reference }}\n'
            '**Challan status after this receipt:** {{ status_after }}\n\n'
            '{{ material_table }}\n\n'
            '{{ damage_section }}\n\n'
            '**Documents:** {{ documents_summary }}'
        ),
        button_label='Open this receipt on the challan',
        recipients_rule=('Every active SCM user and the site\'s assigned PM (coordinators '
                         'are not included), except whoever recorded the GRN; and the '
                         'named recipients below, who receive it even when they recorded '
                         'it. Sent once for each receipt or SCM correction, after it is '
                         'saved; never for imported receipts, nor for a GRN that was '
                         'refused.'),
        default_state='paused',
        deferred=True,
        named_recipients=True,
    ),

    # --- SNAP-1: the daily Site Dashboard snapshot ------------------------------------
    # Sent by the send_dashboard_snapshot command (8 PM IST Railway cron) with the PNG
    # images attached. The code resolves nobody: the named recipients are the whole
    # list (Zuber only, to start). The email starts PAUSED and Admin switches it on
    # after a test send; in-app is never paused, so the one-line notice links to the
    # Site Dashboard to be worth having. NOT deferred (SNAP-1 Q5): no request waits on
    # a cron job, and the command needs the send's outcome for its exit code.
    # No variable carries a money figure (tests_dashboard_snapshot pins the payload).
    NotificationEvent(
        code='daily_dashboard_snapshot',
        label='Site Dashboard daily snapshot',
        module='Dashboard',
        tier=TIER_KNOW,
        channels=('email',),
        variables=(
            Variable('snapshot_date', 'The date of the figures, e.g. 14 Nov 2026',
                     '14 Nov 2026'),
            Variable('taken_at', 'When the figures were collected, in IST',
                     '14 Nov 2026, 8:00 PM IST'),
            Variable('headline', 'Three organisation figures in one line',
                     '142 sites in execution · 77 overdue tasks · 110 gone quiet'),
            Variable('image_count', 'How many images are attached, in words',
                     '2 images'),
        ),
        in_app_text=('Site Dashboard snapshot {{ snapshot_date }}: {{ headline }}. '
                     'Open the Site Dashboard for the detail.'),
        default_subject='Site Dashboard – daily snapshot, {{ snapshot_date }}',
        default_body=('The Site Dashboard figures as at {{ taken_at }} are attached '
                      '({{ image_count }}): {{ headline }}.'),
        button_label='Open the Site Dashboard',
        recipients_rule=('Only the named recipients below; the code names nobody. Sent '
                         'once a day by the send_dashboard_snapshot command (8 PM IST '
                         'cron). Its --to option sends to one person instead of the '
                         'list.'),
        default_state='paused',
        named_recipients=True,
    ),
])


def get_event(event_code):
    """The registered event, or KeyError — an unknown code is a programmer error."""
    try:
        return NOTIFICATION_EVENTS[event_code]
    except KeyError:
        raise KeyError(f'Unknown notification event {event_code!r}') from None
