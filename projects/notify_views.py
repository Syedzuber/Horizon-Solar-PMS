"""
Admin screens for notification templates (T2, Notification Template Management spec v0.1).

Under /portal-admin/notifications/. Every screen and every action is Admin only, by role
(ruling T-1): the wording and the on/off switch of a message sent to the whole company is
an administrative setting, like the master switches. System Admin is refused with every
other role. Each POST action is its own view and redirects with a message; the one
exception is a failed Save in the draft editor, which re-renders the editor with what was
typed and every error (ruling Q1), because a redirect would throw the typing away.

These views decide who may act. What may be done is notify_admin's: every write here is
one call to it, and its refusals arrive as NotificationAdminRefused and become an error
message.

Previews are rendered on the server and shown in <iframe sandbox srcdoc="...">, with no
allow-scripts and no allow-same-origin: even if escaping failed, nothing in the preview
could run or reach this page. Alpine on these screens holds local state only (the
placeholder inserter); nothing here fetches.
"""

import copy
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import notify_admin
from .decorators import login_required, role_required
from .models import (
    NOTIFY_STATE_ACTIVE, NOTIFY_STATE_PAUSED,
    NotificationEventSetting, NotificationEventSettingChange, NotificationLog,
    NotificationTemplate, log_activity,
)
from .notify import RenderError
from .notify_admin import TEST_TEMPLATE_NAME, NotificationAdminRefused
from .notify_registry import NOTIFICATION_EVENTS

LOG_ROWS_SHOWN = 50
COUNT_WINDOW = timedelta(days=7)

# What a skipped or failed test row's error_detail means, and the switch to check (Q6).
# The keys are send_notification()'s own wording; anything else is shown as it is.
_TEST_NOT_SENT = {
    'Master switch off': (
        'email is switched off for the whole system. Turn on "Email notifications '
        '(ZeptoMail)" under Settings → Master switches'),
    'User preference off': (
        'email notifications are off for your own account. Turn on Email for yourself '
        'under Settings → Notification prefs'),
    'Recipient has no email address': (
        'your account has no email address. Add one to your user under Reports → Users'),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event_or_404(event_code, channel=None):
    """The registered event (and, when given, a channel it declares), else 404."""
    event = NOTIFICATION_EVENTS.get(event_code)
    if event is None or (channel is not None and channel not in event.channels):
        raise Http404('No such notification event or channel.')
    return event


def _version_or_404(event, channel, pk):
    """One version of this event and channel; a pk from another event is a 404, so a
    URL cannot pair one event's page with another's version."""
    return get_object_or_404(
        NotificationTemplate.objects.select_related('created_by__user', 'updated_by__user'),
        pk=pk, event_code=event.code, channel=channel)


def _event_url(event):
    return reverse('admin_notification_event', args=[event.code])


def _draft_url(template, preview=False):
    url = reverse('admin_notification_draft',
                  args=[template.event_code, template.channel, template.pk])
    return f'{url}?preview=1' if preview else url


def _report_test(request, log_row):
    """Turn the test's log row into one message saying exactly what happened (Q6)."""
    if log_row is None:
        messages.error(request, 'The test was not recorded. Check the application log.')
    elif log_row.status == 'sent':
        messages.success(request, f'Test sent to {log_row.recipient.user.email}. '
                                  f'It counts only until the wording is saved again.')
    else:
        reason = _TEST_NOT_SENT.get(log_row.error_detail, log_row.error_detail)
        messages.error(request, f'Test not sent: {reason}, then send the test again. '
                                f'Nothing can be activated or switched on until a test '
                                f'is sent.')


def _preview(event, template):
    """(subject, html, error) for `template` (None = code default) with sample values."""
    try:
        subject, _, html = notify_admin.render_sample(event, template)
        return subject, html, ''
    except RenderError as exc:
        return '', '', str(exc)


# ---------------------------------------------------------------------------
# B1 — events list
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def admin_notification_events(request):
    """Every registered event, grouped by module, with each channel's state, live content
    and last seven days of sends. Access: Admin only (T-1). GET only.

    Three queries whatever the number of events: settings, active versions and log
    counts, each for all events at once.
    """
    codes = list(NOTIFICATION_EVENTS)
    settings_by = {(s.event_code, s.channel): s for s in
                   NotificationEventSetting.objects.filter(event_code__in=codes)}
    live_by = {(t.event_code, t.channel): t.version_no for t in
               NotificationTemplate.objects.filter(
                   event_code__in=codes, status=NotificationTemplate.ACTIVE)
               .only('event_code', 'channel', 'version_no')}
    # One grouped query for every event and channel: counts over the last seven days and
    # the newest 'sent'. Test sends are left out (ruling Q2): they are the Admin trying
    # the wording, not the event reaching anybody. order_by() clears Meta.ordering so it
    # cannot leak into the GROUP BY.
    since = timezone.now() - COUNT_WINDOW
    recent = Q(created_at__gte=since)
    stats_by = {(r['event_code'], r['channel']): r for r in
                NotificationLog.objects.filter(event_code__in=codes)
                .exclude(template_name=TEST_TEMPLATE_NAME)
                .values('event_code', 'channel').order_by()
                .annotate(sent=Count('pk', filter=recent & Q(status='sent')),
                          failed=Count('pk', filter=recent & Q(status='failed')),
                          skipped=Count('pk', filter=recent & Q(status='skipped')),
                          last_sent=Max('created_at', filter=Q(status='sent')))}

    modules = {}
    for event in NOTIFICATION_EVENTS.values():
        channels = []
        for channel in event.channels:
            setting = settings_by.get((event.code, channel))
            stats = stats_by.get((event.code, channel), {})
            channels.append({
                'channel': channel,
                'active': setting.is_active if setting else event.default_state == 'active',
                'is_default_state': setting is None,
                'live_version': live_by.get((event.code, channel)),
                'last_sent': stats.get('last_sent'),
                'sent': stats.get('sent', 0),
                'failed': stats.get('failed', 0),
                'skipped': stats.get('skipped', 0),
            })
        modules.setdefault(event.module, []).append({'event': event, 'channels': channels})

    groups = [(module, sorted(rows, key=lambda r: r['event'].label))
              for module, rows in sorted(modules.items())]
    return render(request, 'projects/admin/notifications/list.html', {'groups': groups})


# ---------------------------------------------------------------------------
# B2 — one event
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def admin_notification_event(request, event_code):
    """One event: recipients rule, variables, and per channel its state and switch,
    live content preview, test send, draft, version history, switch history; then the
    event's last 50 log rows. Access: Admin only (T-1). GET only.

    A fixed number of queries for the page, plus two per declared channel (live
    content and its test status), however many versions, switches or log rows exist.
    """
    event = _event_or_404(event_code)
    settings_by = {s.channel: s for s in
                   NotificationEventSetting.objects.filter(event_code=event.code)}
    # Every version of every channel in one query; split per channel below.
    versions = list(NotificationTemplate.objects.filter(event_code=event.code)
                    .select_related('created_by__user', 'updated_by__user')
                    .order_by('channel', '-version_no'))
    changes = list(NotificationEventSettingChange.objects.filter(event_code=event.code)
                   .select_related('changed_by__user'))
    # The newest 50 rows of this event on every channel, in-app included (Q2), tests
    # included and marked. select_related: each row shows its recipient's name.
    logs = list(NotificationLog.objects.filter(event_code=event.code)
                .select_related('recipient__user').order_by('-created_at')[:LOG_ROWS_SHOWN])

    channels = []
    for channel in event.channels:
        chan_versions = [v for v in versions if v.channel == channel]
        live = next((v for v in chan_versions if v.status == NotificationTemplate.ACTIVE), None)
        draft = next((v for v in chan_versions if v.status == NotificationTemplate.DRAFT), None)
        setting = settings_by.get(channel)
        subject, html, error = _preview(event, live)
        channels.append({
            'channel': channel,
            'active': setting.is_active if setting else event.default_state == 'active',
            'is_default_state': setting is None,
            'live': live,
            'draft': draft,
            'versions': chan_versions,
            'changes': [c for c in changes if c.channel == channel],
            'preview_subject': subject,
            'preview_html': html,
            'preview_error': error,
            'live_tested': notify_admin.live_content_tested(event.code, channel),
        })

    return render(request, 'projects/admin/notifications/event.html', {
        'event': event,
        'channels': channels,
        'logs': logs,
        'test_template_name': TEST_TEMPLATE_NAME,
    })


@login_required
@role_required(['Admin'])
def admin_notification_set_state(request, event_code, channel):
    """Switch an event's channel on or off, with a reason. Access: Admin only (T-1).
    POST only. Switching email on is refused until the live content was test-sent (A7)."""
    event = _event_or_404(event_code, channel)
    if request.method != 'POST':
        return redirect(_event_url(event))
    by = request.user.profile
    is_active = request.POST.get('state') == NOTIFY_STATE_ACTIVE
    reason = request.POST.get('reason', '')
    try:
        setting = notify_admin.set_state(event.code, channel, is_active, by, reason)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_event_url(event))
    to_state = NOTIFY_STATE_ACTIVE if is_active else NOTIFY_STATE_PAUSED
    # Cut to ActivityLog.action's 255 characters; the full reason is on the
    # NotificationEventSettingChange row set_state() wrote.
    log_activity(None, by, (f"Notification '{event.label}' ({channel}) switched {to_state}: "
                            f"{reason.strip()}")[:255],
                 entity_type='NotificationEventSetting', entity_id=setting.pk)
    messages.success(request, f'{event.label} ({channel}) is now {to_state}.')
    return redirect(_event_url(event))


@login_required
@role_required(['Admin'])
def admin_notification_test_live(request, event_code, channel):
    """Email the live content (active version, else the built-in default) to the Admin
    asking, with sample values. Access: Admin only (T-1). POST only."""
    event = _event_or_404(event_code, channel)
    if request.method != 'POST':
        return redirect(_event_url(event))
    try:
        row = notify_admin.send_test(event.code, channel, request.user.profile)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
    else:
        _report_test(request, row)
    return redirect(_event_url(event))


@login_required
@role_required(['Admin'])
def admin_notification_draft_create(request, event_code, channel):
    """Start a draft from the live content. Access: Admin only (T-1). POST only.
    Refused while another draft is open (A1)."""
    event = _event_or_404(event_code, channel)
    if request.method != 'POST':
        return redirect(_event_url(event))
    try:
        draft = notify_admin.create_draft(event.code, channel, request.user.profile)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_event_url(event))
    messages.success(request, f'Draft version {draft.version_no} created.')
    return redirect(_draft_url(draft))


# ---------------------------------------------------------------------------
# B3 — draft editor
# ---------------------------------------------------------------------------

def _render_editor(request, event, template, *, values=None, errors=(), status=200):
    """The editor page; `values` are what was typed when a Save failed (Q1).

    The change note starts EMPTY on every visit: each save must say why that edit was
    made. The stored note (create_draft()'s automatic one, or the last save's) is shown
    beside the field, not pre-filled into it, so it cannot satisfy "required" unread.
    """
    values = values or {
        'subject': template.subject, 'body': template.body,
        'button_label': template.button_label, 'change_note': '',
    }
    preview_subject = preview_html = preview_error = ''
    if request.GET.get('preview') and not errors:
        preview_subject, preview_html, preview_error = _preview(event, template)
    return render(request, 'projects/admin/notifications/draft.html', {
        'event': event,
        'template': template,
        'values': values,
        'errors': list(errors),
        'preview_subject': preview_subject,
        'preview_html': preview_html,
        'preview_error': preview_error,
        'tested': notify_admin.version_tested(template),
        'body_max': notify_admin.BODY_MAX_CHARS,
    }, status=status)


@login_required
@role_required(['Admin'])
def admin_notification_draft(request, event_code, channel, pk):
    """The draft editor. Access: Admin only (T-1). GET only; a version that is no longer
    a draft opens read-only instead. ?preview=1 adds the sandboxed preview."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    if template.status != NotificationTemplate.DRAFT:
        return redirect('admin_notification_version', event.code, channel, template.pk)
    return _render_editor(request, event, template)


@login_required
@role_required(['Admin'])
def admin_notification_draft_save(request, event_code, channel, pk):
    """Save a draft's wording; with action=preview, then show its preview. Access: Admin
    only (T-1). POST only. Validated first: on any error nothing is saved and the editor
    is re-rendered with what was typed and every error (Q1)."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    if request.method != 'POST':
        return redirect(_draft_url(template))
    values = {field: request.POST.get(field, '') for field in
              ('subject', 'body', 'button_label', 'change_note')}

    # Validate an unsaved copy, so the stored draft is untouched when anything is wrong.
    candidate = copy.copy(template)
    candidate.subject, candidate.body = values['subject'], values['body']
    candidate.button_label = values['button_label']
    errors = notify_admin.validate_template(candidate)
    if not values['change_note'].strip():
        errors.append('Change note: say what changed and why.')
    if len(values['subject']) > NotificationTemplate._meta.get_field('subject').max_length:
        errors.append('Subject: at most 255 characters.')
    if len(values['button_label']) > NotificationTemplate._meta.get_field('button_label').max_length:
        errors.append('Button label: at most 60 characters.')
    if errors:
        return _render_editor(request, event, template, values=values, errors=errors)

    try:
        notify_admin.update_draft(template, request.user.profile, values['subject'],
                                  values['body'], values['button_label'],
                                  values['change_note'])
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_event_url(event))
    messages.success(request, f'Draft version {template.version_no} saved. Send a test '
                              f'before activating it.')
    return redirect(_draft_url(template, preview=request.POST.get('action') == 'preview'))


@login_required
@role_required(['Admin'])
def admin_notification_draft_test(request, event_code, channel, pk):
    """Email this draft to the Admin asking, with sample values (A5). Access: Admin only
    (T-1). POST only. Sends the SAVED wording: unsaved typing is not part of the test."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    if request.method != 'POST':
        return redirect(_draft_url(template))
    try:
        row = notify_admin.send_test(event.code, channel, request.user.profile,
                                     template=template)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
    else:
        _report_test(request, row)
    return redirect(_draft_url(template))


@login_required
@role_required(['Admin'])
def admin_notification_draft_activate(request, event_code, channel, pk):
    """Make this draft the live wording. Access: Admin only (T-1). POST only. Refused
    unless it validates and was test-sent after its last save (A6)."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    if request.method != 'POST':
        return redirect(_draft_url(template))
    by = request.user.profile
    try:
        notify_admin.activate(template, by)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_draft_url(template))
    log_activity(None, by, f"Notification '{event.label}' ({channel}) version "
                           f"{template.version_no} activated",
                 entity_type='NotificationTemplate', entity_id=template.pk)
    messages.success(request, f'Version {template.version_no} is live.')
    return redirect(_event_url(event))


@login_required
@role_required(['Admin'])
def admin_notification_draft_discard(request, event_code, channel, pk):
    """Discard this draft: deleted if never test-sent, else kept as discarded (A3).
    Access: Admin only (T-1). POST only."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    if request.method != 'POST':
        return redirect(_event_url(event))
    by = request.user.profile
    version_no = template.version_no
    try:
        outcome = notify_admin.discard_draft(template, by)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_event_url(event))
    log_activity(None, by, f"Notification '{event.label}' ({channel}) draft version "
                           f"{version_no} {outcome}",
                 entity_type='NotificationTemplate',
                 entity_id=template.pk if outcome == 'discarded' else None)
    if outcome == 'deleted':
        messages.success(request, f'Draft version {version_no} deleted.')
    else:
        messages.success(request, f'Draft version {version_no} was test-sent, so it is '
                                  f'kept in the history as discarded.')
    return redirect(_event_url(event))


# ---------------------------------------------------------------------------
# B4 — one version, read-only; restore
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def admin_notification_version(request, event_code, channel, pk):
    """One version, read-only: wording, preview and history fields. Access: Admin only
    (T-1). GET only."""
    event = _event_or_404(event_code, channel)
    template = _version_or_404(event, channel, pk)
    subject, html, error = _preview(event, template)
    return render(request, 'projects/admin/notifications/version.html', {
        'event': event,
        'template': template,
        'preview_subject': subject,
        'preview_html': html,
        'preview_error': error,
    })


@login_required
@role_required(['Admin'])
def admin_notification_version_restore(request, event_code, channel, pk):
    """Copy an old version's wording into a new draft (rollback). Access: Admin only
    (T-1). POST only. Refused while another draft is open (A1)."""
    event = _event_or_404(event_code, channel)
    source = _version_or_404(event, channel, pk)
    if request.method != 'POST':
        return redirect(_event_url(event))
    by = request.user.profile
    try:
        draft = notify_admin.create_draft(event.code, channel, by, from_version=source)
    except NotificationAdminRefused as exc:
        messages.error(request, str(exc))
        return redirect(_event_url(event))
    log_activity(None, by, f"Notification '{event.label}' ({channel}) version "
                           f"{source.version_no} restored as draft version {draft.version_no}",
                 entity_type='NotificationTemplate', entity_id=draft.pk)
    messages.success(request, f'Version {source.version_no} copied into draft version '
                              f'{draft.version_no}. Test it, then activate it.')
    return redirect(_draft_url(draft))
