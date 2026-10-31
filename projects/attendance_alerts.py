"""
Contractor attendance — ATT-B4: the "no attendance logged" alert.

Which live sites have installation work in progress but no attendance today, who is told
about which of them, and the words the message carries. Nothing here sends: the command
send_attendance_alerts does, through notify() for in-app and email and through
send_notification() for WhatsApp.

    sites_missing_attendance(today)   the sites, in code order, ONE query
    group_by_recipient(sites)         {(profile, role): [site, ...]} for PMs and SCM
    sites_without_pm(sites)           the sites no PM will be told about
    format_site_list(sites)           "A, B and N more", one line
    format_site_lines(sites)          "A – <link>" per line, for the email
    channels_sent_today(day, people)  {profile pk: {channel, ...}} already sent

A SITE IS MISSING ATTENDANCE (ruling 1) when it is live (ATTENDANCE_SITE_STATUSES, not
soft-deleted), at least one applicable, human-owned task in its Installation phase is In
Progress at run time (Blocked and Not Started do not count), and it has no non-voided
ContractorAttendance dated `today`.

INSTALLATION PHASE IDENTITY is phase_rules' (PHASE_INSTALLATION): ProjectPhase has no link
to its template phase, so a phase is Installation when any template task in it comes from
the template's INSTALLATION phase. A hand-added task in that phase counts; a phase holding
only hand-added tasks has no identity and is never Installation. Both the OPEX and the
Residential templates have one; CAPEX has no template, so CAPEX sites never qualify.
"""
from django.conf import settings
from django.db.models import Exists, OuterRef
from django.urls import reverse

from .models import ContractorAttendance, NotificationLog, Project, Task
from .notify import absolute_url
from .notify_admin import TEST_TEMPLATE_NAME
from .permissions import ATTENDANCE_SITE_STATUSES, active_scm_users, site_assigned_pm
from .phase_rules import PHASE_INSTALLATION
from .utils import applicable_tasks_q, human_owned_tasks_q

EVENT_CODE = 'attendance_missing'

#: The Interakt template the command sends; also NotificationLog.template_name of the
#: WhatsApp rows. The per-channel duplicate check reads event_code, not this.
WHATSAPP_TEMPLATE = 'contractor_attendance_missing'

ROLE_PM, ROLE_SCM = 'PM', 'SCM'


def sites_missing_attendance(today):
    """The sites missing attendance on `today` (ruling 1), in project-code order.

    ONE query whatever the number of sites: both conditions are correlated EXISTS
    subqueries on the Project row. The phases are found by a nested subquery rather than
    by joining Task to its template task, so a hand-added task in an Installation phase
    counts (see the module docstring). Mirrors are left out: a delivery mirror moved by a
    challan is not crew work on site. assigned_pm and its user come with the row, for
    group_by_recipient().
    """
    installation_phases = (Task.objects
                           .filter(template_task__phase__code=PHASE_INSTALLATION)
                           .values('phase_id'))
    work_in_progress = (Task.objects
                        .filter(phase__project=OuterRef('pk'),
                                phase_id__in=installation_phases,
                                status=Task.IN_PROGRESS)
                        .filter(applicable_tasks_q(), human_owned_tasks_q()))
    logged_today = ContractorAttendance.objects.filter(
        project=OuterRef('pk'), date=today, is_voided=False)
    return list(Project.objects
                .filter(is_deleted=False, status__in=ATTENDANCE_SITE_STATUSES)
                .filter(Exists(work_in_progress), ~Exists(logged_today))
                .select_related('assigned_pm__user')
                .order_by('project_id'))


def group_by_recipient(sites):
    """{(profile, role): [site, ...]} for everyone told about `sites` (ruling 4).

    Each site's assigned PM, alone (site_assigned_pm — no coordinators), with all of that
    PM's sites; then every active SCM user with every site. Deactivated people are left
    out here, not only by notify(), because the WhatsApp leg does not go through notify().
    PMs come first, then SCM, each by name, so the command's output reads the same on
    every run. One query (the SCM users) beyond the sites themselves.

    Somebody who is both a site's PM and an SCM user gets two entries; the command's
    per-channel duplicate check then sends them only the first.
    """
    if not sites:
        return {}
    by_pm = {}
    for site in sites:
        pm = site_assigned_pm(site)
        if pm is not None:
            by_pm.setdefault(pm, []).append(site)
    groups = {}
    for pm in sorted(by_pm, key=_name_key):
        groups[(pm, ROLE_PM)] = by_pm[pm]
    for scm in sorted(active_scm_users().select_related('user'), key=_name_key):
        groups[(scm, ROLE_SCM)] = list(sites)
    return groups


def sites_without_pm(sites):
    """The sites no PM is told about: no assigned PM, or a deactivated one (edge case 6)."""
    return [site for site in sites if site_assigned_pm(site) is None]


def _name_key(profile):
    return ((profile.user.get_full_name() or profile.user.username).lower(), profile.pk)


def _code(site):
    # A WhatsApp parameter may not hold a line break (ruling 6); collapsing whitespace
    # keeps a code typed with one from breaking the message.
    return ' '.join(str(site.project_id).split())


def _limit(limit):
    return settings.ATTENDANCE_ALERT_SITE_LIST_LIMIT if limit is None else limit


def format_site_list(sites, limit=None):
    """'A, B, C and N more': at most `limit` codes (ATTENDANCE_ALERT_SITE_LIST_LIMIT),
    comma-separated, in the order given, on one line."""
    codes = [_code(site) for site in sites]
    limit = _limit(limit)
    shown, rest = codes[:limit], len(codes) - limit
    if rest > 0:
        return f"{', '.join(shown)} and {rest} more"
    return ', '.join(shown)


def site_attendance_url(site):
    """The absolute link to `site`'s attendance page, on APP_BASE_URL (absolute_url)."""
    return absolute_url(reverse('attendance_today', args=[site.project_id]))


def format_site_lines(sites, limit=None):
    """The email's list (ruling R5): one line per site, 'CODE – <attendance URL>', at
    most `limit` of them, then 'and N more'. Multi-line on purpose: notify() turns each
    line break in a value into <br>, and mail clients make the bare URLs clickable."""
    limit = _limit(limit)
    lines = [f'{_code(site)} – {site_attendance_url(site)}' for site in sites[:limit]]
    rest = len(sites) - limit
    if rest > 0:
        lines.append(f'and {rest} more')
    return '\n'.join(lines)


def first_name(profile):
    """The WhatsApp greeting's name: the first word of the full name, else the username."""
    name = (profile.user.get_full_name() or profile.user.username).split()
    return name[0] if name else profile.user.username


def channels_sent_today(day, profiles):
    """{profile pk: {channel, ...}}: the channels each of `profiles` already has a 'sent'
    row on for this event on `day` (ruling R2: the check is per channel).

    created_at__date resolves in TIME_ZONE (IST) under USE_TZ, so `day` is compared with
    the IST date of the row, never the UTC one. Admin's test sends carry the event code
    too and are excluded by their template_name. One query; read-only, so a dry run shows
    exactly what a real run would skip. Not a lock: two runs started at the same moment
    could both send, as for the morning report; the cron fires once a day.
    """
    sent = {}
    rows = (NotificationLog.objects
            .filter(event_code=EVENT_CODE, status='sent', created_at__date=day,
                    recipient__in=list(profiles))
            .exclude(template_name=TEST_TEMPLATE_NAME)
            .values_list('recipient_id', 'channel'))
    for recipient_id, channel in rows:
        sent.setdefault(recipient_id, set()).add(channel)
    return sent
