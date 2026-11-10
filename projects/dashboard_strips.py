"""
DASH-B4 — the role strips the Site Dashboard carries over from the retired PM, SCM and
Finance dashboards.

WHY THIS MODULE EXISTS. The figures below were computed INLINE in views.dashboard_pm,
views.dashboard_scm and views.dashboard_finance, not in helpers the Site Dashboard could
call. Zuber's ruling (10 Nov) was to move each one into a helper the Site Dashboard uses,
and leave the old views' own copies in place, each marked with a TODO, until those views
are deleted after a quiet week (a separate commit). Until then the two copies must say the
same thing: change the rule here, and the old copy dies with its view.

Every function here is a fixed number of queries whatever the number of sites. Who may see
each strip is decided by views_site_dashboard (role checks there, with their reasons);
nothing here checks a role.

    pm_awaiting_reverification()   PM/coordinator "Awaiting re-verification"   3 queries + 1
    scm_tile_counts()              SCM tiles: BOQ awaiting, deliveries today, overdue DCs   3
    scm_pipeline()                 SCM stall pill + stage line per site                     4
    scm_open_delivery_issues()     SCM "Open delivery issues" group                         1
    milestones_awaiting_count()    Finance "Milestones awaiting" tile                       1
"""
from django.db.models import Count, Min

from .models import BOQ, DeliveryChallan, Issue, PaymentMilestone, Project
from .permissions import manageable_projects_q
from .punch_points import awaiting_reverification
# views.py does not import this module, so importing its two shared delivery helpers is
# not circular (qaqc_views and attendance_views import from views the same way).
from .views import _build_delivery_lookup, _project_material_badge

#: The project statuses the SCM tiles count, as on the old SCM dashboard.
_LIVE_STATUSES = ['Active', 'In Progress']

#: SCM stall thresholds in days (amber, red), exactly the old dashboard's: a BOQ awaiting
#: acknowledgement past 2 / 5 days, an order (open DC) past 3 / 7, a delivery overdue or
#: unscheduled past 5 / 10. Any open issue is a stall, red once the oldest is past 2 days.
STALL_LIMITS = {'boq': (2, 5), 'order': (3, 7), 'delivery': (5, 10)}
ISSUE_RED_AFTER = 2

#: How many open delivery issues the SCM group lists, as the old table did.
DELIVERY_ISSUES_SHOWN = 20


# ---------------------------------------------------------------------------
# PM and coordinator
# ---------------------------------------------------------------------------

def pm_awaiting_reverification(profile):
    """The PM dashboard's "Awaiting re-verification" list (Closeout 7b, PP-6), moved from
    views.dashboard_pm: every Rectified punch point on a site `profile` manages, oldest
    first, each with `latest_round` and `pm_may_reverify`.

    pm_may_reverify is the rule the old dashboard drew the button from: the PM may
    re-verify only where no QA/QC engineer can act on the site (go-ahead Q4) and they did
    not record the latest rectify round themselves (Q5). The re-verify endpoint asks both
    again; this only decides whether the row says "Re-verify" or "Open".

    The managed sites are manageable_projects_q() over every status, the same set the old
    dashboard passed (it applied no status filter either). One query for the ids, then
    punch_points.awaiting_reverification()'s three.
    """
    # The sites this PM or coordinator manages, as pks: assigned PM or coordinator, the
    # queryset form of user_can_manage_project(). distinct() because the coordinators leg
    # is an M2M join that can repeat a project.
    managed = list(Project.objects.filter(manageable_projects_q(profile))
                   .values_list('pk', flat=True).distinct())
    points = awaiting_reverification(managed)
    for point in points:
        rounds = point.rectify_rounds
        point.latest_round = rounds[0] if rounds else None
        point.pm_may_reverify = (not point.site_has_engineer
                                 and (point.latest_round is None
                                      or point.latest_round.actor_id != profile.pk))
    return points


# ---------------------------------------------------------------------------
# SCM
# ---------------------------------------------------------------------------

def scm_tile_counts(today):
    """The SCM dashboard's three header tiles, moved from views.dashboard_scm with its
    default (no context) scope, so the numbers match what SCM saw there:

        boq_awaiting      BOQs Submitted and not yet acknowledged, any project (the old
                          query carried no project filter at all, and is kept as it was)
        deliveries_today  DCs expected today and not fully received, on Active / In
                          Progress projects that are not soft-deleted
        overdue_dcs       DCs past their expected date and still Expected, Partial or
                          Rejected (Rejected is the severe case), same projects

    Three COUNT queries.
    """
    boq_awaiting = BOQ.objects.filter(status='Submitted').count()
    # project__is_deleted=False: a soft-deleted project keeps its status (often Active),
    # so the status filter alone would count its challans.
    live_challans = DeliveryChallan.objects.filter(
        project__is_deleted=False, project__status__in=_LIVE_STATUSES)
    deliveries_today = live_challans.filter(
        expected_delivery_date=today,
        status__in=[DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED],
    ).count()
    overdue_dcs = live_challans.filter(
        expected_delivery_date__lt=today,
        expected_delivery_date__isnull=False,
        status__in=[DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
                    DeliveryChallan.REJECTED],
    ).count()
    return {'boq_awaiting': boq_awaiting, 'deliveries_today': deliveries_today,
            'overdue_dcs': overdue_dcs}


def _stall(boq_status, boq_age, order_status, order_age, delivery_status, delivery_age,
           issue_count, issue_age):
    """The old dashboard's stall verdict: (stage, days, 'red' | 'amber') for the worst
    stalled stage, or None. Red beats amber; within a level the most days wins."""
    candidates = []

    def judge(stage, days):
        amber, red = STALL_LIMITS[stage]
        if days > red:
            candidates.append((stage, days, 'red'))
        elif days > amber:
            candidates.append((stage, days, 'amber'))

    if boq_status == 'Submitted' and boq_age is not None:
        judge('boq', boq_age)
    if order_status in ('Expected', 'Partial', 'Rejected') and order_age is not None:
        judge('order', order_age)
    if delivery_status in ('Overdue', 'Not Scheduled') and delivery_age is not None:
        judge('delivery', delivery_age)
    if issue_count > 0:
        age = issue_age or 0
        candidates.append(('issues', age, 'red' if age > ISSUE_RED_AFTER else 'amber'))
    if not candidates:
        return None
    red = [c for c in candidates if c[2] == 'red']
    return max(red or candidates, key=lambda c: c[1])


def scm_pipeline(sites, today):
    """The SCM dashboard's per-site stage badges and stall clock, moved from
    views.dashboard_scm. `sites` is an iterable of (pk, project_id). Returns
    {pk: row} with, for each site:

        boq_status, boq_age            the BOQ's status; days since submission while Submitted
        order_status, order_age        None / Received / Expected / Partial / Rejected (the
                                       worst open DC); days since the oldest open DC
        delivery_status, delivery_age  None / Received / Overdue / Not Scheduled / Scheduled;
                                       days overdue, or days unscheduled
        issue_count, issue_age         open issues (Open or In Progress, all of them, as the
                                       old badge counted) and the oldest one's age in days
        stall                          (stage, days, level) for the worst stalled stage, or None

    Four queries for any number of sites: BOQs, DCs, the DC line aggregate
    (views._build_delivery_lookup) and the issue aggregate. A site with no BOQ, DC or
    issue still gets a row.
    """
    sites = list(sites)
    if not sites:
        return {}
    pids = [project_id for _, project_id in sites]

    # Each site's BOQ (one per project), keyed by the string project_id the loop uses.
    boqs = {b.project.project_id: b
            for b in BOQ.objects.filter(project__project_id__in=pids).select_related('project')}
    # Every DC on these sites, newest DC date first within a site; grouped in Python.
    challans = {}
    for challan in (DeliveryChallan.objects.filter(project__project_id__in=pids)
                    .select_related('project').order_by('project__project_id', '-dc_date')):
        challans.setdefault(challan.project.project_id, []).append(challan)
    delivery_lookup = _build_delivery_lookup(pids)
    # Open issues per site with the oldest raised_at, one grouped query. order_by() clears
    # Issue's default ordering, which would otherwise join the GROUP BY.
    issues = {
        row['project__project_id']: row
        for row in (Issue.objects
                    .filter(project__project_id__in=pids,
                            status__in=[Issue.OPEN, Issue.IN_PROGRESS])
                    .values('project__project_id')
                    .annotate(count=Count('pk'), oldest_at=Min('raised_at')).order_by())
    }

    rows = {}
    for pk, pid in sites:
        boq = boqs.get(pid)
        site_challans = challans.get(pid, [])

        boq_status = boq.status if boq else None
        boq_age = None
        if boq and boq.status == 'Submitted' and boq.submitted_at:
            boq_age = (today - boq.submitted_at.date()).days

        pending = [c for c in site_challans if c.status in (
            DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
            DeliveryChallan.REJECTED)]
        if not site_challans:
            order_status, order_age = None, None
        elif not pending:
            order_status, order_age = 'Received', None
        else:
            rank = {DeliveryChallan.REJECTED: 3, DeliveryChallan.PARTIALLY_RECEIVED: 2,
                    DeliveryChallan.EXPECTED: 1}
            worst = max(pending, key=lambda c: rank.get(c.status, 0))
            order_status = ('Rejected' if worst.status == DeliveryChallan.REJECTED else
                            'Partial' if worst.status == DeliveryChallan.PARTIALLY_RECEIVED
                            else 'Expected')
            order_age = (today - min(c.dc_date for c in pending)).days

        if _project_material_badge(pid, delivery_lookup) == 'Received':
            delivery_status, delivery_age = 'Received', None
        else:
            overdue = [c for c in site_challans
                       if c.expected_delivery_date and c.expected_delivery_date < today
                       and c.status != DeliveryChallan.RECEIVED]
            unscheduled = [c for c in site_challans
                           if not c.expected_delivery_date
                           and c.status != DeliveryChallan.RECEIVED]
            if overdue:
                delivery_status = 'Overdue'
                delivery_age = max((today - c.expected_delivery_date).days for c in overdue)
            elif unscheduled:
                delivery_status = 'Not Scheduled'
                delivery_age = (today - min(c.dc_date for c in unscheduled)).days
            elif site_challans:
                delivery_status, delivery_age = 'Scheduled', None
            else:
                delivery_status, delivery_age = None, None

        issue = issues.get(pid)
        issue_count = issue['count'] if issue else 0
        issue_age = ((today - issue['oldest_at'].date()).days
                     if issue and issue['oldest_at'] else None)

        rows[pk] = {
            'boq_status': boq_status, 'boq_age': boq_age,
            'order_status': order_status, 'order_age': order_age,
            'delivery_status': delivery_status, 'delivery_age': delivery_age,
            'issue_count': issue_count, 'issue_age': issue_age,
            'stall': _stall(boq_status, boq_age, order_status, order_age,
                            delivery_status, delivery_age, issue_count, issue_age),
        }
    return rows


def scm_open_delivery_issues():
    """The SCM dashboard's "Open Delivery Issues" table, moved from views.dashboard_scm:
    the newest DELIVERY_ISSUES_SHOWN Open or In Progress issues raised against a DC on an
    Active or In Progress project, with the project, the DC and the raiser joined for the
    row's links. One query."""
    return list(
        Issue.objects.filter(
            delivery_challan__project__status__in=_LIVE_STATUSES,
            status__in=[Issue.OPEN, Issue.IN_PROGRESS],
        )
        .select_related('project', 'delivery_challan', 'raised_by__user')
        .order_by('-raised_at')[:DELIVERY_ISSUES_SHOWN]
    )


# ---------------------------------------------------------------------------
# Finance
# ---------------------------------------------------------------------------

def milestones_awaiting_count():
    """The Finance dashboard's "Milestones Awaiting" tile, moved from
    views.dashboard_finance with its default (no context) scope: Pending client payment
    milestones on Active or In Progress projects. One COUNT query."""
    return PaymentMilestone.objects.filter(
        project__status__in=_LIVE_STATUSES,
        status=PaymentMilestone.PENDING,
    ).count()
