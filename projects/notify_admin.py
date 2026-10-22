"""
Notification template and switch service functions (T1; the T2 screens call these).

The ONLY writers of NotificationTemplate, NotificationEventSetting and
NotificationEventSettingChange. No views live here and nothing here checks who the
caller is: the screens in notify_views.py decide that (Admin only). What these functions
refuse is what is wrong whoever asks — an unknown event, a channel the event does not
declare, editing a non-draft, a second open draft, activating text that would not render
or was never test-sent, switching an event on whose live content was never test-sent, a
switch without a reason.

TEST SENDS (T2, A5-A7, rulings P2 and P3). send_test() emails the Admin who asks, with
every variable at its SAMPLE value, and logs under template_name 'notify_test'. A test
counts only when its log row is 'sent' and:
  * for a version: it names that version and was sent after the version's last save
    (updated_at, else created_at) — activate() needs one (A6);
  * for the live content when switching the event on (A7): the active version, sent after
    activated_at; or, with no active version, the code default, matched by the text body
    it logged equalling the code default rendered with samples now. A change to the
    default wording in code therefore makes earlier tests stop counting.

Refusals raise NotificationAdminRefused with a sentence a screen can show.
"""

from django.db import transaction
from django.db.models import Max, ProtectedError
from django.utils import timezone

from .models import (
    NOTIFY_CHANNEL_EMAIL, NOTIFY_STATE_ACTIVE, NOTIFY_STATE_PAUSED,
    NotificationEventSetting, NotificationEventSettingChange, NotificationLog,
    NotificationTemplate,
)
from .notifications import send_notification
from .notify import RenderError, active_template, channel_is_active, render_email
from .notify_registry import IDENTIFIER_RE, NOTIFICATION_EVENTS, placeholder_names

BODY_MAX_CHARS = 5000

# NotificationLog.template_name of every test send, and the subject's prefix.
TEST_TEMPLATE_NAME  = 'notify_test'
TEST_SUBJECT_PREFIX = '[TEST] '


class NotificationAdminRefused(Exception):
    """A template or switch change that is refused, with the reason as its message."""


def _event_for(event_code, channel):
    """The registered event, refusing an unknown code or a channel it does not declare."""
    event = NOTIFICATION_EVENTS.get(event_code)
    if event is None:
        raise NotificationAdminRefused(f'There is no notification event {event_code!r}.')
    if channel not in event.channels:
        raise NotificationAdminRefused(
            f'{event.label} is not sent on {channel!r}; its channels are '
            f'{", ".join(event.channels)}.')
    return event


def open_draft(event_code, channel):
    """The one open draft for the event and channel, or None."""
    return NotificationTemplate.objects.filter(
        event_code=event_code, channel=channel, status=NotificationTemplate.DRAFT).first()


def create_draft(event_code, channel, by, *, from_version=None):
    """A new draft version for the event and channel, numbered after the highest.

    Its content is copied from `from_version` when given (a rollback: the old wording
    comes back as a NEW version, so the history still shows what was live in between),
    else from the active version, else from the registry's code default. Refused while
    another draft is open (A1).
    """
    event = _event_for(event_code, channel)
    if from_version is not None and (from_version.event_code, from_version.channel) != (
            event_code, channel):
        raise NotificationAdminRefused(
            f'{from_version} belongs to a different event or channel.')

    with transaction.atomic():
        existing = open_draft(event_code, channel)
        if existing is not None:
            raise NotificationAdminRefused(
                f'Version {existing.version_no} is already an open draft. Edit it, '
                f'activate it or discard it before starting another.')
        # Max + 1 and the open-draft check are read without a lock: two drafts created
        # at the same moment would collide on uniq_notification_template_version or
        # uniq_draft_notification_template, and the second fails with an IntegrityError
        # rather than producing a duplicate.
        highest = NotificationTemplate.objects.filter(
            event_code=event_code, channel=channel).aggregate(n=Max('version_no'))['n'] or 0
        source = from_version or active_template(event_code, channel)
        if source is not None:
            fields = dict(subject=source.subject, body=source.body,
                          button_label=source.button_label,
                          wa_template_name=source.wa_template_name,
                          wa_language=source.wa_language, wa_slots=list(source.wa_slots))
            note = (f'Rollback: copy of version {source.version_no}' if from_version
                    else f'Draft from version {source.version_no}')
        else:
            fields = dict(subject=event.default_subject, body=event.default_body,
                          button_label=event.button_label)
            note = 'Draft from the code default'
        return NotificationTemplate.objects.create(
            event_code=event_code, channel=channel, version_no=highest + 1,
            status=NotificationTemplate.DRAFT, created_by=by, change_note=note, **fields)


def update_draft(template, by, subject, body, button_label, change_note):
    """Change a draft's wording, stamping who and when (A2). Refused for any other status.

    Does not validate: the editor validates an unsaved copy first and saves nothing on
    error (ruling Q1), and activate() validates again. A draft may therefore hold
    wording that would not render; it can never go live with it.
    """
    _event_for(template.event_code, template.channel)
    change_note = (change_note or '').strip()
    if not change_note:
        raise NotificationAdminRefused('Say what changed and why.')
    if not template.is_editable:
        raise NotificationAdminRefused(
            f'Version {template.version_no} is {template.status}; only a draft can be '
            f'edited. Create a new draft instead.')
    template.subject = subject or ''
    template.body = body or ''
    template.button_label = button_label or ''
    template.change_note = change_note
    template.updated_by = by
    template.updated_at = timezone.now()
    # save() asks _require_draft_template() against the stored row as well, so a
    # version activated by someone else since `template` was loaded is still refused.
    template.save()
    return template


def discard_draft(template, by):
    """Remove an open draft (A3). Returns 'deleted' or 'discarded'.

    A draft nobody test-sent is deleted. One that was test-sent cannot be: its test's log
    row points at it (PROTECT), and that row is the record of what was emailed. It is
    marked discarded instead, frozen, never served, and no longer blocks a new draft.
    """
    _event_for(template.event_code, template.channel)
    if template.status != NotificationTemplate.DRAFT:
        raise NotificationAdminRefused(
            f'Version {template.version_no} is {template.status}; only a draft can be '
            f'discarded.')
    try:
        with transaction.atomic():
            template.delete()
        return 'deleted'
    except ProtectedError:
        pass
    # filter().update() on status=draft rather than load-and-save: if the draft was
    # activated between the read above and this write, the update matches no row and
    # the discard is refused instead of overwriting the live version's status.
    marked = NotificationTemplate.objects.filter(
        pk=template.pk, status=NotificationTemplate.DRAFT,
    ).update(status=NotificationTemplate.DISCARDED)
    if not marked:
        raise NotificationAdminRefused(
            f'Version {template.version_no} is no longer a draft; nothing was discarded.')
    template.status = NotificationTemplate.DISCARDED
    return 'discarded'


def validate_template(template):
    """Every reason `template` cannot be activated, as sentences; [] when it can."""
    event = _event_for(template.event_code, template.channel)
    errors = []
    declared = set(event.variable_names)
    for label, text in (('Subject', template.subject), ('Body', template.body)):
        for name in placeholder_names(text):
            if not IDENTIFIER_RE.match(name):
                errors.append(f'{label}: unknown placeholder {{{{ {name} }}}}. A '
                              f'placeholder is a single name, like {{{{ recipient_name }}}}.')
            elif name not in declared:
                errors.append(f'{label}: {{{{ {name} }}}} is not a variable of this event. '
                              f'Available: {", ".join(sorted(declared))}.')
    if template.channel == NOTIFY_CHANNEL_EMAIL and not (template.subject or '').strip():
        errors.append('Subject: an email needs a subject.')
    if len(template.body or '') > BODY_MAX_CHARS:
        errors.append(f'Body: {len(template.body)} characters; the limit is '
                      f'{BODY_MAX_CHARS:,}.')
    return errors


def render_sample(event, template):
    """(subject, text, html) of `template` (None = code default) with sample values."""
    return render_email(event, template, event.samples(), '/')


def render_preview(template):
    """(subject, text, html) for `template` filled with the event's sample values.

    Raises RenderError (from notify) when the wording would not render; a screen shows
    validate_template()'s sentences instead of calling this on an invalid draft.
    """
    event = _event_for(template.event_code, template.channel)
    return render_sample(event, template)


def send_test(event_code, channel, by, *, template=None):
    """Email `template` (None = the live content) to `by`, with sample values (A5).

    The live content is the active version, else the code default. The event's own
    switch is ignored — a paused event is tested before it is switched on — but the
    email master switch and `by`'s own email preference apply, through
    send_notification(). actor is None, so notify()'s rule of dropping the actor never
    applies: the Admin is the recipient.

    Sends at once, not after commit: the Admin is waiting to be told what happened.
    Returns the NotificationLog row the send wrote, whose status says what happened.
    """
    event = _event_for(event_code, channel)
    # Only email can be tested: no event declares WhatsApp yet, and a WhatsApp test
    # needs an approved Interakt template (ruling Q8).
    if channel != NOTIFY_CHANNEL_EMAIL:
        raise NotificationAdminRefused('Test sends are available for email only.')
    if template is not None:
        if (template.event_code, template.channel) != (event_code, channel):
            raise NotificationAdminRefused(
                f'{template} belongs to a different event or channel.')
        # A draft is tested before activation and the active version is the live
        # content; an archived or discarded version is history. Its rollback path is
        # "Restore as new draft", then a test of that draft.
        if template.status in (NotificationTemplate.ARCHIVED, NotificationTemplate.DISCARDED):
            raise NotificationAdminRefused(
                f'Version {template.version_no} is {template.status} and cannot be '
                f'test-sent. Restore it as a new draft, then test that draft.')
        errors = validate_template(template)
        if errors:
            raise NotificationAdminRefused(' '.join(errors))
    else:
        template = active_template(event_code, channel)
    if by is None or not (by.is_active and by.user.is_active):
        raise NotificationAdminRefused('Your account is not active, so a test cannot be sent to it.')
    try:
        subject, text, html = render_sample(event, template)
    except RenderError as exc:
        raise NotificationAdminRefused(f'The wording does not render: {exc}.') from None

    mark = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
    send_notification(
        recipient=by,
        message=text,
        channels=[NOTIFY_CHANNEL_EMAIL],
        subject=TEST_SUBJECT_PREFIX + subject,
        html_message=html,
        template=TEST_TEMPLATE_NAME,
        actor=None,
        event_code=event.code,
        template_version=template,
    )
    # The row send_notification() just wrote. It does not return it; this send's row is
    # the newest test row for this recipient and event after the mark.
    return NotificationLog.objects.filter(
        pk__gt=mark, recipient=by, template_name=TEST_TEMPLATE_NAME, event_code=event.code,
    ).order_by('-pk').first()


def _sent_tests(event_code, channel, template):
    """'sent' test rows of `template` (None = rows that named no version)."""
    rows = NotificationLog.objects.filter(
        template_name=TEST_TEMPLATE_NAME, event_code=event_code, channel=channel,
        status='sent')
    if template is None:
        return rows.filter(template_version__isnull=True)
    return rows.filter(template_version=template)


def version_tested(template):
    """A6 + P3: a sent test of this exact version, after its last save."""
    since = template.updated_at or template.created_at
    return _sent_tests(template.event_code, template.channel, template).filter(
        created_at__gt=since).exists()


def live_content_tested(event_code, channel):
    """A7: a sent test of what the event would send now.

    The active version, tested after it was activated; or, with none, the code default,
    tested with the exact text it renders to today (P2).
    """
    event = _event_for(event_code, channel)
    live = active_template(event_code, channel)
    if live is not None:
        since = live.activated_at or live.updated_at or live.created_at
        return _sent_tests(event_code, channel, live).filter(created_at__gt=since).exists()
    try:
        _, text, _ = render_sample(event, None)
    except RenderError:
        return False
    return _sent_tests(event_code, channel, None).filter(message=text).exists()


def activate(template, by):
    """Make a draft the live wording, archiving the previous active version.

    Refused unless it validates and was test-sent after its last save (A6).
    """
    _event_for(template.event_code, template.channel)
    if template.status != NotificationTemplate.DRAFT:
        raise NotificationAdminRefused(
            f'Version {template.version_no} is {template.status}; only a draft can be '
            f'activated.')
    errors = validate_template(template)
    if errors:
        raise NotificationAdminRefused(' '.join(errors))
    with transaction.atomic():
        # Locks the draft while its test is checked and it is activated, so a save of
        # its wording cannot land between the two: update_draft()'s UPDATE waits, then
        # finds the version active and is refused by the save() guard.
        locked = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != NotificationTemplate.DRAFT:
            raise NotificationAdminRefused(
                f'Version {locked.version_no} is no longer a draft.')
        if not version_tested(locked):
            raise NotificationAdminRefused(
                f'Send a test of version {locked.version_no} to yourself first. A test '
                f'counts only if it was sent after the draft was last saved.')
        locked.activate()
    template.status, template.activated_at = locked.status, locked.activated_at
    return template


def set_state(event_code, channel, is_active, by, reason):
    """Switch an event's channel on or off, with a reason, and record the change.

    The setting row and its history row are written in one transaction. Switching to
    the state it is already in is refused, so the history holds changes only. Switching
    email on is refused until the live content has been test-sent (A7, ruling T-4).
    """
    event = _event_for(event_code, channel)
    reason = (reason or '').strip()
    if not reason:
        raise NotificationAdminRefused('Give a reason for the change.')
    to_state = NOTIFY_STATE_ACTIVE if is_active else NOTIFY_STATE_PAUSED

    with transaction.atomic():
        # Locks the setting row while the from-state is read and the new state written,
        # so two Admins switching at once are serialised and each history row's
        # from_state is the state it really changed. When there is no row yet nothing is
        # locked; two concurrent first switches collide on uniq_notification_event_setting
        # and the second fails with an IntegrityError instead of recording a false
        # from_state.
        setting = NotificationEventSetting.objects.select_for_update().filter(
            event_code=event_code, channel=channel).first()
        was_active = setting.is_active if setting else channel_is_active(event, channel)
        from_state = NOTIFY_STATE_ACTIVE if was_active else NOTIFY_STATE_PAUSED
        if from_state == to_state:
            raise NotificationAdminRefused(f'{event.label} ({channel}) is already {to_state}.')
        if (is_active and channel == NOTIFY_CHANNEL_EMAIL
                and not live_content_tested(event_code, channel)):
            raise NotificationAdminRefused(
                'Send a test of the live content to yourself first. For an active '
                'version the test must be sent after it was activated.')

        if setting is None:
            setting = NotificationEventSetting(event_code=event_code, channel=channel)
        setting.is_active = bool(is_active)
        setting.changed_by = by
        setting.reason = reason
        setting.save()
        NotificationEventSettingChange.objects.create(
            event_code=event_code, channel=channel, from_state=from_state,
            to_state=to_state, changed_by=by, reason=reason)
    return setting
