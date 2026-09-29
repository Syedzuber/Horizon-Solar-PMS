"""
Approvals 2c — the read-only queries behind the pending-approvals cards and the aging
list (27 Sep 2026). Nothing here writes.

The dashboards import ONE function each, pending_approvals_card(); the aging page reads
aging_rows(). Both read STEP ROWS through approvals.exclude_carried_steps(), never the
ledger: the step row is the accountability record (who was asked, when their turn came,
who decided, who typed it), and a carried step is not a fresh decision (D-A20).

WHO A FIGURE BELONGS TO is the step's `assignee` — the person SCM named, as the model
says. A deputy's decision on the Design Head's step counts toward the Head; the aging
list shows how many of a person's decisions were made that way ("Decided by deputy").

DAYS WAITING are whole 24-hour periods since `activated_at` — when the step's turn came,
not when the request was raised, so a contractor bill's PM is not charged for the Site
Engineer's time. Under 24 hours reads "today". Elapsed time, so no timezone enters it.

No import of approval_forms: it imports views, and views imports this module.
"""
from datetime import timedelta
from statistics import median

from django.db.models import F, Prefetch, Q, prefetch_related_objects
from django.utils import timezone

from .approvals import exclude_carried_steps
from .models import (
    ApprovalRequest, ApprovalStep, Program, SiteGroup, SiteGroupMembership,
    APPROVAL_OPEN, APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_DECISIONS, APPROVAL_STEP_PENDING,
)
from .permissions import APPROVAL_ASSIGNEE_ROLES, user_has_design_head_authority

#: The aging list's look-back for decisions, turnaround, proxy share and carry rate.
AGING_WINDOW_DAYS = 90

_DAY = timedelta(days=1)
_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)
# Roles that can be NAMED on a step. The design party is the is_design_head flag, which
# user_has_design_head_authority() covers along with the deputy.
_APPROVER_ROLES = frozenset().union(*APPROVAL_ASSIGNEE_ROLES.values())


def _person(profile):
    """approval_forms.person_name(), which this module cannot import (see above)."""
    return profile.user.get_full_name() or profile.user.username


def days_waiting(activated_at, now=None):
    """Whole 24-hour periods from `activated_at` to `now`. 0 means under a day."""
    now = now or timezone.now()
    return max((now - activated_at) // _DAY, 0)


def days_text(days):
    if days < 1:
        return 'today'
    return '1 day' if days == 1 else f'{days} days'


def live_pending_steps():
    """Every step waiting on someone NOW: pending, its turn has come, its request open,
    in the request's current round, and not carried. The queryset form of
    permissions._approval_step_is_live(). Superseded steps are not pending, so they
    never appear."""
    return exclude_carried_steps(ApprovalStep.objects.filter(
        verdict=APPROVAL_STEP_PENDING, activated_at__isnull=False,
        request__status=APPROVAL_OPEN, round=F('request__current_round')))


def _pending_row(step, now, viewer_pk=None):
    approval = step.request
    days = days_waiting(step.activated_at, now)
    return {
        'step':          step,
        'approval':      approval,
        'title':         approval.title,
        'kind_label':    approval.get_kind_display(),
        'vendor_name':   approval.vendor.name if approval.vendor_id else '—',
        'party_label':   _PARTY_LABELS.get(step.party, step.party),
        'round':         step.round,
        'days':          days,
        'days_text':     days_text(days),
        # Set only on a row the viewer sees because they are the assignee Head's deputy.
        'as_deputy_for': (_person(step.assignee)
                          if viewer_pk is not None and step.assignee_id != viewer_pk
                          else None),
    }


def pending_steps_for(profile, now=None):
    """The steps waiting on `profile`, oldest first — ONE query.

    Their own (they are the assignee), plus design steps whose assignee is a Design Head
    who has named `profile` as deputy. The Head flag is re-checked on the assignee, as
    permissions.user_is_design_head_deputy() does: a deputy is only a deputy of an
    actual Head. Each deputy row carries `as_deputy_for`, the Head's name."""
    now = now or timezone.now()
    steps = (live_pending_steps()
             .filter(Q(assignee=profile)
                     | Q(party=APPROVAL_PARTY_DESIGN, assignee__is_design_head=True,
                         assignee__design_head_deputy=profile))
             .select_related('request__vendor', 'assignee__user')
             .order_by('activated_at', 'pk'))
    return [_pending_row(step, now, viewer_pk=profile.pk) for step in steps]


def pending_approvals_card(user, now=None):
    """The dashboard card's context: None for somebody who approves nothing.

    Shown to anyone who has a step waiting, and — so the "Nothing waiting" state can
    show — to anyone who could be asked: a PM or Site Engineer by role, or Design Head
    authority (the Head or a deputy). A plain designer or a Project Coordinator with
    nothing waiting gets None, and the partial draws nothing.

    One query for the rows, plus at most one for the deputy check."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return None
    rows = pending_steps_for(profile, now)
    if not rows and not (profile.role in _APPROVER_ROLES
                         or user_has_design_head_authority(user)):
        return None
    return {
        'rows':        rows,
        'count':       len(rows),
        'oldest_text': rows[0]['days_text'] if rows else '',
    }


def aging_rows(since, now=None):
    """The aging list, in THREE queries whatever the number of people or requests.

      Q1  every live pending step (live_pending_steps), oldest first
      Q2  every non-carried decision since `since`
      Q3  the count of carried steps since `since`

    Returns {'assignees': [...], 'carried': n, 'fresh_approved': n, 'carry_rate': x}.

    Per assignee: `pending` (their rows, oldest first), `pending_count`, `oldest_days`;
    `decisions` since `since`; `median_days`, the median of decided_at − activated_at in
    days; `proxy_share`, the share of decisions SCM recorded on their behalf;
    `by_deputy`, decisions whose decider is not the assignee. None where there were no
    decisions. People with something waiting come first, longest wait first; the rest
    by name.

    CARRY RATE: carried ÷ (carried + freshly decided approvals), rounds 2 and later only
    — round 1 has nothing to carry. A carried step is always approved and never in
    round 1, so Q3 needs neither term. None when there is nothing to divide."""
    now = now or timezone.now()
    people = {}

    def person(profile):
        entry = people.get(profile.pk)
        if entry is None:
            entry = people[profile.pk] = {
                'profile': profile, 'name': _person(profile), 'pending': [],
                'turnarounds': [], 'decisions': 0, 'proxies': 0, 'by_deputy': 0,
            }
        return entry

    # Q1
    for step in (live_pending_steps()
                 .select_related('request__vendor', 'assignee__user')
                 .order_by('activated_at', 'pk')):
        person(step.assignee)['pending'].append(_pending_row(step, now))

    # Q2
    fresh_approved = 0
    for step in (exclude_carried_steps(ApprovalStep.objects.filter(
                     verdict__in=APPROVAL_STEP_DECISIONS, decided_at__gte=since))
                 .select_related('assignee__user')):
        entry = person(step.assignee)
        entry['decisions'] += 1
        entry['proxies'] += step.is_proxy
        entry['by_deputy'] += step.decided_by_id != step.assignee_id
        if step.activated_at is not None:
            entry['turnarounds'].append((step.decided_at - step.activated_at) / _DAY)
        if step.verdict == APPROVAL_STEP_APPROVED and step.round >= 2:
            fresh_approved += 1

    # Q3
    carried = ApprovalStep.objects.filter(carried_from__isnull=False,
                                          decided_at__gte=since).count()

    assignees = []
    for entry in people.values():
        pending = entry.pop('pending')
        turnarounds = entry.pop('turnarounds')
        decisions = entry['decisions']
        oldest = pending[0]['days'] if pending else None
        entry.update({
            'pending':       pending,
            'pending_count': len(pending),
            'oldest_days':   oldest,
            'oldest_text':   days_text(oldest) if pending else '—',
            'median_days':   round(median(turnarounds), 1) if turnarounds else None,
            'proxy_share':   entry['proxies'] / decisions if decisions else None,
            'by_deputy':     entry['by_deputy'] if decisions else None,
            '_first':        pending[0]['step'].activated_at if pending else None,
        })
        assignees.append(entry)
    assignees.sort(key=lambda e: (e['_first'] is None, e['_first'] or now, e['name']))
    for entry in assignees:
        del entry['_first']

    denominator = carried + fresh_approved
    return {
        'assignees':      assignees,
        'carried':        carried,
        'fresh_approved': fresh_approved,
        'carry_rate':     carried / denominator if denominator else None,
    }


# ---------------------------------------------------------------------------
# S6 — the CEO Tenders "Waiting on someone" card (29 Sep 2026)
#
# A SCOPE, NEVER A NEW DEFINITION. "Waiting" is live_pending_steps() and the days are
# days_waiting()/days_text(), exactly as the aging list and the pending cards read them;
# this section only narrows which requests count. No median turnaround: no approvals
# screen shows one across people, so the card could not match one (S6 ruling C).
#
# `sites_qs` is views.tender_sites_qs(), passed in because this module cannot import
# views (see the module docstring).
# ---------------------------------------------------------------------------

#: How many of the oldest waiting steps the card lists.
TENDER_WAITING_ROWS = 5


def tender_approval_requests(sites_qs):
    """The pks of approval requests about a live tender site, as a subquery.

    A request is in when it links to at least one site in `sites_qs` directly
    (`projects`), through a site group with a LIVE membership of such a site (a removed
    membership is history, not scope), or through a program that has such a site. A link
    to test-only sites or programs therefore never brings a request in. The three M2M
    joins repeat a request's pk, which is harmless inside an IN (…)."""
    live_groups = SiteGroup.objects.filter(memberships__removed_at__isnull=True,
                                           memberships__project__in=sites_qs)
    return (ApprovalRequest.objects
            .filter(Q(projects__in=sites_qs)
                    | Q(site_groups__in=live_groups)
                    | Q(programs__in=sites_qs.values('program')))
            .values('pk'))


def _unlinked_requests():
    """Requests with no scope link at all — no program, project or site group. A request
    linked only to test or non-tender sites is NOT unlinked: it is about something, just
    not a tender, so it stays out of the card entirely."""
    linked = (ApprovalRequest.objects
              .filter(Q(programs__isnull=False) | Q(projects__isnull=False)
                      | Q(site_groups__isnull=False))
              .values('pk'))
    return ApprovalRequest.objects.exclude(pk__in=linked).values('pk')


def _scope_label(approval):
    """"MPUVNL · 3 sites", the site code when the request reaches one site, or the
    program name(s) alone when it is linked only to whole programs.

    Sites are those reached through `projects` and live group memberships, read from the
    prefetches tender_approvals_waiting() set up, so this issues no query. Programs are
    the sites' own plus any linked program with a live site, comma-joined by name."""
    sites = {site.pk: site for site in approval.projects.all()}
    for group in approval.site_groups.all():
        for membership in group.memberships.all():
            sites[membership.project.pk] = membership.project
    names = {program.name for program in approval.programs.all()}
    names.update(site.program.name for site in sites.values() if site.program_id)
    if len(sites) == 1:
        return next(iter(sites.values())).project_id
    programs = ', '.join(sorted(names))
    if not sites:
        return programs
    count = f'{len(sites)} sites'
    return f'{programs} · {count}' if programs else count


def tender_approvals_waiting(sites_qs, now=None):
    """The CEO Tenders card: approval steps waiting on someone, tenders only.

    QUERIES, whatever the number of requests or sites: 1 for every scoped live step,
    1 for the unlinked count, and — only when there are rows — 4 prefetches for the scope
    labels of the TENDER_WAITING_ROWS rows shown (projects, programs, site groups, their
    live memberships). So 2 or 6.

    Returns:
      count        scoped steps waiting (the aging list's "steps waiting", scoped)
      oldest_days  whole days the oldest has waited; None when nothing waits
      oldest_text  days_text() of it, '—' when nothing waits
      rows         the oldest TENDER_WAITING_ROWS steps, ordered activated_at, pk as
                   aging_rows() orders them: _pending_row() plus `waiting_on` (the
                   assignee — the Head on a design step, as the aging list names him)
                   and `scope_label`
      unlinked     open requests with a live step and no scope link at all
    """
    now = now or timezone.now()
    # Every scoped live step, oldest first. The whole list rather than count() + [:5]:
    # one query instead of two, and pending steps number in the tens.
    steps = list(live_pending_steps()
                 .filter(request__in=tender_approval_requests(sites_qs))
                 .select_related('request__vendor', 'assignee__user')
                 .order_by('activated_at', 'pk'))
    shown = steps[:TENDER_WAITING_ROWS]
    # Scope links narrowed to what the label may name: live tender sites only, programs
    # with one, live memberships of one. Prefetched on the shown rows alone, so the cost
    # stays four queries however many steps wait.
    prefetch_related_objects(
        shown,
        Prefetch('request__projects', queryset=sites_qs.select_related('program')),
        Prefetch('request__programs',
                 queryset=Program.objects.filter(pk__in=sites_qs.values('program'))),
        Prefetch('request__site_groups__memberships',
                 queryset=(SiteGroupMembership.objects
                           .filter(removed_at__isnull=True, project__in=sites_qs)
                           .select_related('project__program'))),
    )
    rows = []
    for step in shown:
        row = _pending_row(step, now)
        row['waiting_on'] = _person(step.assignee)
        row['scope_label'] = _scope_label(step.request)
        rows.append(row)

    # A request counts once however many of its steps wait (a PM and a Design Head in
    # parallel are one request). order_by() clears Meta.ordering, whose columns would
    # otherwise join the DISTINCT and count a request once per step.
    unlinked = (live_pending_steps()
                .filter(request__in=_unlinked_requests())
                .order_by().values('request').distinct().count())

    oldest = rows[0]['days'] if rows else None
    return {
        'count':       len(steps),
        'oldest_days': oldest,
        'oldest_text': rows[0]['days_text'] if rows else '—',
        'rows':        rows,
        'unlinked':    unlinked,
    }
