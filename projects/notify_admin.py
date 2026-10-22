"""
Notification template and switch service functions (T1; the T2 screens call these).

The ONLY writers of NotificationTemplate, NotificationEventSetting and
NotificationEventSettingChange. No views live here and nothing here checks who the
caller is: the screens that call these functions decide that (T2). What these functions
refuse is what is wrong whoever asks — an unknown event, a channel the event does not
declare, editing a non-draft, activating text that would not render, a switch without a
reason.

Refusals raise NotificationAdminRefused with a sentence a screen can show.
"""

from django.db import transaction
from django.db.models import Max

from .models import (
    NOTIFY_CHANNEL_EMAIL, NOTIFY_STATE_ACTIVE, NOTIFY_STATE_PAUSED,
    NotificationEventSetting, NotificationEventSettingChange, NotificationTemplate,
)
from .notify import active_template, channel_is_active, render_email
from .notify_registry import IDENTIFIER_RE, NOTIFICATION_EVENTS, placeholder_names

BODY_MAX_CHARS = 5000


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


def create_draft(event_code, channel, by, *, from_version=None):
    """A new draft version for the event and channel, numbered after the highest.

    Its content is copied from `from_version` when given (a rollback: the old wording
    comes back as a NEW version, so the history still shows what was live in between),
    else from the active version, else from the registry's code default.
    """
    event = _event_for(event_code, channel)
    if from_version is not None and (from_version.event_code, from_version.channel) != (
            event_code, channel):
        raise NotificationAdminRefused(
            f'{from_version} belongs to a different event or channel.')

    with transaction.atomic():
        # Max + 1 is read and written without a lock: two drafts created at the same
        # moment would pick the same number, and uniq_notification_template_version
        # refuses the second with an IntegrityError rather than duplicating it.
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
    """Change a draft's wording. Refused for an active or archived version (R-7)."""
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
    # save() asks _require_draft_template() against the stored row as well, so a
    # version activated by someone else since `template` was loaded is still refused.
    template.save()
    return template


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


def activate(template, by):
    """Make a draft the live wording, archiving the previous active version."""
    _event_for(template.event_code, template.channel)
    errors = validate_template(template)
    if errors:
        raise NotificationAdminRefused(' '.join(errors))
    return template.activate()


def set_state(event_code, channel, is_active, by, reason):
    """Switch an event's channel on or off, with a reason, and record the change.

    The setting row and its history row are written in one transaction. Switching to
    the state it is already in is refused, so the history holds changes only.
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


def render_preview(template):
    """(subject, text, html) for `template` filled with the event's sample values.

    Raises RenderError (from notify) when the wording would not render; a screen shows
    validate_template()'s sentences instead of calling this on an invalid draft.
    """
    event = _event_for(template.event_code, template.channel)
    return render_email(event, template, event.samples(), '/')
