"""
RPT-B2a — the Reports section's pages: the list of reports (level 1) and one generated
report (level 2), with the first report on it, Active users.

    /reports/                          reports_home     the report cards (D2)
    /reports/<slug>/?period=…&date=…   report_detail    one report for one period (D3)

Layout follows the approved prototype docs/prototypes/rpt-prototype.html, drawn in this
app's Bootstrap shell (base.html) rather than the prototype's Tailwind.

WHERE THE FIGURES COME FROM (D8). Every number is read from report_activity — B1's
activity_report() and accounts_by_role() — and this module only SHAPES it: picks rows,
sorts, works out a percentage. No query is written here or in the templates; a figure
B1 does not return is a new helper in report_activity.py with its own test, never a
query in a view. So the page's query count is the helpers' count, fixed whatever the
period's length or the number of sites (tests_report_pages pins it).

THE REGISTRY. REPORTS maps a slug to what a report is: title, description, the "How it's
counted" definition, its template and a builder. A builder takes a report_periods.Period
and returns one plain context dict; report_detail adds the period, the parse error,
"Generated HH:MM" and "by". B2b adds reports 2 and 3 as two more entries.

READ-ONLY. Nothing in this module writes.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .decorators import _forbidden, login_required
from .permissions import user_can_view_reports
from .report_activity import accounts_by_role, action_kind_label, activity_report
from .report_periods import (PERIOD_DATE, PERIOD_MONTH, PERIOD_RANGE, PERIOD_TODAY,
                             PERIOD_WEEK, PERIOD_YESTERDAY, REPORTS_START, parse_period)


#: The period chips, in the order every report card and report header shows them (D2).
PERIOD_CHOICES = (
    (PERIOD_TODAY, 'Today'),
    (PERIOD_YESTERDAY, 'Yesterday'),
    (PERIOD_WEEK, 'This week'),
    (PERIOD_MONTH, 'This month'),
    (PERIOD_DATE, 'On date'),
    (PERIOD_RANGE, 'Date range'),
)
PERIOD_KEY_SET = frozenset(k for k, _ in PERIOD_CHOICES)

#: Site labels shown in a "Who was active" row before "+N more"; the rest are inside a
#: <details>, so a user with 140 sites keeps a one-line row (edge case 2).
SITES_SHOWN = 6

#: Action kinds shown in the "What" column: the top three, as the spec asks.
KINDS_SHOWN = 3

#: The words in a tile that never compares (D4): open tasks are counted as of now, so a
#: "vs yesterday" beside them would compare today's count with today's count.
AS_OF_NOW = 'as of now'

NO_ACTIVITY = 'No activity in this period'

#: Tile tones. GOOD/BAD colour a comparison by whether its direction is welcome.
TONE_GOOD, TONE_BAD, TONE_FLAT = 'good', 'bad', 'flat'


# ---------------------------------------------------------------------------
# Shaping helpers (pure Python over B1's results)
# ---------------------------------------------------------------------------

def compare(current, previous, previous_label, up_is_good=True):
    """The comparison line under a tile (D4), or None when there is no previous period.

    Returns {'arrow', 'word', 'text', 'tone', 'label'}: '▲' / '▼' (with 'up' / 'down' for
    screen readers, which would otherwise read the glyph's name) and '23%', 'no change', or
    'was 0' when the previous figure is 0 (a percentage of nothing is meaningless; ruling
    on the pre-flight). `label` is B1's previous-period label ('vs 26 Nov 2026'), used as
    it is so every report compares against the same words (report_periods D3)."""
    if previous is None:
        return None
    if current == previous:
        return {'arrow': '', 'word': '', 'text': 'no change', 'tone': TONE_FLAT,
                'label': previous_label}
    up = current > previous
    tone = TONE_GOOD if up == up_is_good else TONE_BAD
    if previous == 0:
        return {'arrow': '▲', 'word': 'up', 'text': 'was 0', 'tone': tone,
                'label': previous_label}
    pct = round(abs(current - previous) / previous * 100)
    return {'arrow': '▲' if up else '▼', 'word': 'up' if up else 'down', 'text': f'{pct}%',
            'tone': tone, 'label': previous_label}


def tile(label, value, comparison=None, as_of_now=False, note='', tone=''):
    """One tile's context. An as-of-now tile never carries a comparison (D4)."""
    return {'label': label, 'value': value, 'note': note, 'tone': tone,
            'as_of_now': as_of_now, 'compare': None if as_of_now else comparison}


def _local(moment):
    return timezone.localtime(moment) if moment else None


def _site_list(site_ids, sites):
    """{'shown', 'more', 'all'} for a Sites cell: the first SITES_SHOWN labels, how many
    more there are, and every label for the <details>. Ids are already in label order."""
    labels = [sites[pk].label for pk in site_ids if pk in sites]
    return {'shown': labels[:SITES_SHOWN], 'more': max(0, len(labels) - SITES_SHOWN),
            'all': labels, 'count': len(labels)}


def _bulk_action_text(action_code):
    """'design_allocated + design_qc_assigned' (BulkAction.action_code) as words:
    'Allocated + QC assigned'. The table's own column says these are bulk actions."""
    return ' + '.join(action_kind_label(code) for code in action_code.split(' + '))


# ---------------------------------------------------------------------------
# Report 1: Active users
# ---------------------------------------------------------------------------

def build_active_users(period):
    """The Active users report's context for `period`: 4 tiles and 5 tables.

    Two helper calls and nothing else: activity_report() (10 queries, both periods) and
    accounts_by_role() (1). Everything below rearranges what they returned."""
    report = activity_report(period)
    cur, prev = report.current, report.previous
    prev_label = period.previous.label if period.previous else ''
    users = cur.active_users

    tiles = [
        tile('Active users', users.users,
             compare(users.users, prev.active_users.users if prev else None, prev_label),
             tone=TONE_GOOD),
        # More people signing in without acting is not welcome, so "up" reads red.
        tile('Signed in, no action', len(cur.signed_in_no_action),
             compare(len(cur.signed_in_no_action),
                     len(prev.signed_in_no_action) if prev else None, prev_label,
                     up_is_good=False),
             note='Opened PMS, changed nothing'),
        tile('Open tasks, no activity', len(cur.open_tasks_no_activity), as_of_now=True,
             note='Holding open tasks, no action in the period', tone=TONE_BAD),
        tile('Actions', users.actions,
             compare(users.actions, prev.active_users.actions if prev else None, prev_label)),
    ]

    # By role: every role with an active account, including roles nobody in which acted
    # (Q4), idle roles first (Active % ascending, then the role name).
    accounts = accounts_by_role()
    acted = {r.role: r for r in users.by_role}
    by_role = []
    for role in set(accounts) | set(acted):
        n_accounts = accounts.get(role, 0)
        total = acted.get(role)
        n_users = total.users if total else 0
        pct = round(n_users / n_accounts * 100) if n_accounts else None
        by_role.append({'role': role, 'users': n_users,
                        'actions': total.actions if total else 0,
                        'accounts': n_accounts, 'pct': pct})
    # A role with active users but no counted account (not possible while both read the
    # D11 rule, kept safe) sorts last rather than raising on None.
    by_role.sort(key=lambda r: (r['pct'] is None, r['pct'] if r['pct'] is not None else 0,
                                r['role']))

    who = [{'name': r.name, 'role': r.role, 'actions': r.actions,
            'sites': _site_list(r.site_ids, cur.sites),
            'bulk_only_sites': r.bulk_only_site_count,
            'no_site': r.actions_without_site,
            'kinds': r.kinds[:KINDS_SHOWN]}
           for r in sorted(users.rows, key=lambda r: (-r.actions, r.name.lower()))]

    # B1 already sorts these: overdue descending, then open descending, then name.
    idle = [{'name': r.name, 'role': r.role, 'open': r.open_tasks,
             'overdue': r.overdue_tasks, 'last': _local(r.last_action_at)}
            for r in cur.open_tasks_no_activity]

    signed_in = [{'name': r.name, 'role': r.role, 'sign_ins': r.sign_ins,
                  'last': _local(r.last_signed_in_at)} for r in cur.signed_in_no_action]

    bulk = [{'name': b.actor_name, 'action': _bulk_action_text(b.action_code),
             'started': _local(b.started_at), 'sites': _site_list(b.site_ids, cur.sites)}
            for b in cur.bulk_actions]

    return {'tiles': tiles, 'by_role': by_role, 'who': who, 'idle': idle,
            'signed_in': signed_in, 'bulk': bulk,
            'actions_without_site': users.actions_without_site}


@dataclass(frozen=True)
class ReportSpec:
    slug: str
    title: str
    description: str              # the card's one line
    definition: str               # "How it's counted", on the card and under the title
    template: str
    builder: Callable             # Period -> context dict
    icon: str = 'file-earmark-text'   # Bootstrap Icons name, without the "bi-" prefix


REPORTS = {spec.slug: spec for spec in (
    ReportSpec(
        slug='active-users',
        title='Active users',
        description='Who used PMS in the period, by role, and who has open tasks but did '
                    'nothing.',
        definition='Active = at least one recorded action (an update, upload, approval or '
                   'attendance entry). A sign-in with no action is shown separately. A bulk '
                   'action, one submission across many sites, counts as one action.',
        template='projects/reports/report_active_users.html',
        builder=build_active_users,
        icon='people'),
)}


# ---------------------------------------------------------------------------
# The period form's values
# ---------------------------------------------------------------------------

def _form_values(params, period, today):
    """What the period chips and date inputs show: the values the person sent, so a bad
    range can be corrected in place, else the period being shown. Plain strings for the
    template; never parsed here (parse_period is the one reader)."""
    sent = (params.get('period') or '').strip().lower()
    key = sent if sent in PERIOD_KEY_SET else period.key
    week_ago = today - timedelta(days=6)
    return {
        'period': key,
        'date': (params.get('date') or '').strip()
        or (period.start_date if period.key == PERIOD_DATE else today).isoformat(),
        'from': (params.get('from') or '').strip()
        or (period.start_date if period.key == PERIOD_RANGE else week_ago).isoformat(),
        'to': (params.get('to') or '').strip()
        or (period.end_date if period.key == PERIOD_RANGE else today).isoformat(),
        'max': today.isoformat(),
        # The browser refuses a day before REPORTS_START; parse_period refuses it too.
        'min': REPORTS_START.isoformat(),
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@login_required
@never_cache
@require_safe
def reports_home(request):
    """The list of reports, each with its period chips and a Generate button (D2).
    Access: CEO, Admin and System Admin with an active login and profile. GET only."""
    # Reports list every colleague's activity, which no other role reads (RPT-B1 D1):
    # permissions.user_can_view_reports admits only those three roles, and only while
    # both the login and the profile are active. Everyone else gets the same 403 page
    # the Daily Report's role gate returns, so a probe learns nothing from the answer.
    if not user_can_view_reports(request.user):
        return _forbidden(request)
    today = timezone.localdate()
    # No period is parsed here: every card starts on Today (the prototype's default).
    form = {'period': PERIOD_TODAY, 'date': today.isoformat(),
            'from': (today - timedelta(days=6)).isoformat(), 'to': today.isoformat(),
            'max': today.isoformat(), 'min': REPORTS_START.isoformat()}
    return render(request, 'projects/reports/reports_home.html', {
        'reports': list(REPORTS.values()),
        'form': form,
        'period_choices': PERIOD_CHOICES,
    })


@login_required
@never_cache
@require_safe
def report_detail(request, slug):
    """One report for one period: header, period chips, tiles, then tables (D3).
    Access: CEO, Admin and System Admin with an active login and profile. GET only."""
    # The same gate and refusal as reports_home. Checked BEFORE the slug is looked up, so
    # someone refused cannot tell a real report's address from a made-up one.
    if not user_can_view_reports(request.user):
        return _forbidden(request)
    spec = REPORTS.get(slug)
    if spec is None:
        raise Http404('No such report')

    today = timezone.localdate()
    # parse_period never raises: a bad period comes back as TODAY plus a sentence, shown
    # in red above today's figures (B1 open question 5).
    period, error = parse_period(request.GET, today)
    context = spec.builder(period)
    user = request.user
    context.update({
        'report': spec,
        'period': period,
        'period_error': error,
        'generated_at': timezone.localtime(timezone.now()),
        'generated_by': user.get_full_name() or user.username,
        'form': _form_values(request.GET, period, today),
        'period_choices': PERIOD_CHOICES,
    })
    return render(request, spec.template, context)
