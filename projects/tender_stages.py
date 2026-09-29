"""
THE ONE PLACE A TENDER SITE'S STAGE IS DERIVED (CEO Tenders view, S3).

Every live tender site is in exactly one of eight stages, S0-S7, read from data that
already exists: the site's DesignAssignment, its procurement-group membership and its
activation stamp. Nothing is stored and nothing here writes. The CEO pipeline (S3) reads
it now; the tender cards (S4) and the stuck list (S7) are meant to read the same helper
rather than restating the rule.

DECISION D6 — THE STAGE COMES FROM DATA. A site with no DesignAssignment, or one still at
a survey status, is "No survey yet": it is never counted against Design. A site with a
survey on file that nobody has allocated is Design's backlog (S1).

EACH SITE IS COUNTED ONCE, AT ITS FURTHEST STAGE. Precedence, highest first:

    S7  activated_at is set                       — beats every design and group stage
    S6  live membership of a LOCKED procurement group  — beats every design stage, even
        when the design is no longer `released` (pre-flight decision 2a: furthest wins)
    S0-S5  STATUS_TO_STAGE[DesignAssignment.status], or S0 when there is no row

FIXED QUERY BUDGET. site_stages() and stage_summary() are ONE query each whatever the
number of sites; activated_progress() is one more; tender_cards() is two whatever the
number of programmes. Nothing here may be called per site — tests_tender_stages pins 3
sites and 30 sites to the same count, tests_tender_cards 2x3 and 4x30.

design_metrics is NOT reused, deliberately (pre-flight P1). Its tender_metrics() runs per
programme, counts only sites that have a DesignAssignment, and splits `arka_submitted` on
the current Arka — a read this mapping does not need, since both halves are S2.

design_throughput() (S5) is the exception, and the opposite way round: its figures must
equal the Design Head's, so it feeds its own four batched reads through design_metrics'
and design_analytics' functions unchanged rather than restating any definition.
"""
from collections import namedtuple
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from .design_analytics import STATE_INSUFFICIENT, m_cycle_time
from .design_metrics import effective_commitment, is_overdue, rework_contribution
# latest_design_transition lives in design_views beside the ledger's other readers.
# design_views does not import views or this module, so importing it here is not a cycle.
from .design_views import latest_design_transition
from .models import (
    CHANGE_REQUEST_OPEN_VERDICTS, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM,
    DC_CATEGORY_TO_MIRROR_CODE,
    DESIGN_ALLOCATED, DESIGN_ARKA_REJECTED, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_ARKA,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_AWAITING_SURVEY,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED,
    DESIGN_QC_FAILED, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    DESIGN_WORK_FINISHED_STATUSES,
    GROUP_TYPE_PROCUREMENT, SITE_GROUP_LOCKED,
    DesignAssignment, DesignAttempt, DesignChangeRequest, DueDateCommitment,
    SiteGroupMembership, Task,
)
from .utils import applicable_tasks_q


STAGE_NO_SURVEY      = 'no_survey'        # S0
STAGE_SURVEY_ON_FILE = 'survey_on_file'   # S1
STAGE_IN_DESIGN      = 'in_design'        # S2
STAGE_IN_QC          = 'in_qc'            # S3
STAGE_AWAITING_PM    = 'awaiting_pm'      # S4
STAGE_RELEASED       = 'released'         # S5
STAGE_LOCKED_GROUP   = 'locked_group'     # S6
STAGE_ACTIVATED      = 'activated'        # S7

# Workflow order, S0 first. The pipeline renders in this order and stage_summary() returns
# one entry per member, zeros included, so a missing stage is never a missing row.
STAGES = [
    (STAGE_NO_SURVEY,      'No survey yet'),
    (STAGE_SURVEY_ON_FILE, 'Survey on file, not allocated'),
    (STAGE_IN_DESIGN,      'In design'),
    (STAGE_IN_QC,          'In QC / Head QC'),
    (STAGE_AWAITING_PM,    'Awaiting PM approval'),
    (STAGE_RELEASED,       'Released, not in a locked procurement group'),
    (STAGE_LOCKED_GROUP,   'In a locked procurement group'),
    (STAGE_ACTIVATED,      'Activated — in execution'),
]
STAGE_LABELS = dict(STAGES)

# EVERY DesignAssignment.status, mapped to the one design stage it counts in. The only
# place this mapping lives. tests_tender_stages iterates DESIGN_ASSIGNMENT_STATUS_CHOICES
# and fails on any status missing here, so a new status cannot be counted by accident.
# Looked up by indexing, not .get(): an unmapped status must fail loudly, not land in
# whichever stage a default happened to name. Groups and activation are NOT in this dict;
# they override it in _stage_of().
STATUS_TO_STAGE = {
    DESIGN_AWAITING_SURVEY:      STAGE_NO_SURVEY,
    # DESIGN HOLD IS S0 (pre-flight decision 1). The designer has declared the survey
    # inadequate, so for the CEO the site has no usable survey — D6 read literally. The
    # pipeline's S0 row says how many of its sites are held, so they are not hidden.
    DESIGN_SURVEY_RETURNED:      STAGE_NO_SURVEY,
    DESIGN_AWAITING_ALLOCATION:  STAGE_SURVEY_ON_FILE,
    # A designer is named but no date is agreed: allocated, so no longer S1.
    DESIGN_ALLOCATED:            STAGE_IN_DESIGN,
    DESIGN_DUE_DATE_PROPOSED:    STAGE_IN_DESIGN,
    DESIGN_IN_DESIGN:            STAGE_IN_DESIGN,
    # The Arka half of review stays in S2 (decision 3); S3 is the package review.
    DESIGN_ARKA_SUBMITTED:       STAGE_IN_DESIGN,
    DESIGN_AWAITING_HEAD_ARKA:   STAGE_IN_DESIGN,
    DESIGN_ARKA_REJECTED:        STAGE_IN_DESIGN,
    # Handed in but QC has not started it. design_metrics files this as Design QC's queue;
    # the CEO pipeline counts it as design not yet in review (decision 3).
    DESIGN_ARTIFACTS_UPLOADED:   STAGE_IN_DESIGN,
    # No committed row can carry qc_failed (design_qc_fail opens attempt N+1 in the same
    # atomic block). Mapped for completeness, to where a failed package goes: the designer.
    DESIGN_QC_FAILED:            STAGE_IN_DESIGN,
    DESIGN_IN_QC:                STAGE_IN_QC,
    DESIGN_AWAITING_HEAD_QC:     STAGE_IN_QC,
    # Back with the Design Head, who decides where it goes next — the same bucket
    # design_metrics._classify gives it (decision 1).
    DESIGN_PM_REJECTED:          STAGE_IN_QC,
    DESIGN_AWAITING_PM_APPROVAL: STAGE_AWAITING_PM,
    DESIGN_RELEASED:             STAGE_RELEASED,
}

# The OPEX template's installation phase, by TaskTemplatePhase.code (seeded by 0075). Code,
# not label, for the reason DC_CATEGORY_TO_MIRROR_CODE gives: rewording a label must not
# silently change what counts as installation.
OPEX_INSTALLATION_PHASE_CODE = 'INSTALLATION'

# design_status rides along so the S0 row can say how many of its sites are held and so
# S4 / S7 can tell sites in one stage apart without a second read.
StageInfo = namedtuple('StageInfo', ['stage_key', 'entered_at', 'design_status'])


def _site_rows(sites_qs):
    """ONE query: every column the stage and its entry date are derived from, per site.

    The DesignAssignment columns come through a LEFT JOIN (a reverse OneToOne, so at most
    one row and no fan-out); a site with no assignment reads None for all of them. The
    group lock, the current attempt's QC start and the ledger date are correlated
    subqueries on the same statement, not further queries.
    """
    # Same three terms as permissions.project_boq_is_group_locked(): a removed membership
    # has left the group, group_type is read off the membership (the column the
    # exclusivity constraint is written over), and only a locked group counts. The partial
    # unique constraint allows one live procurement membership per site, so [:1] below
    # cannot pick between two.
    locked = SiteGroupMembership.objects.filter(
        project=OuterRef('pk'), removed_at__isnull=True,
        group_type=GROUP_TYPE_PROCUREMENT, group__status=SITE_GROUP_LOCKED,
    )
    # The CURRENT attempt is the one whose number matches the assignment's pointer, read
    # from the pointer rather than max() so this agrees with design_views and design_metrics.
    current_attempt = DesignAttempt.objects.filter(
        assignment=OuterRef('design_assignment__pk'),
        attempt_number=OuterRef('design_assignment__current_attempt_number'),
    )
    return list(
        sites_qs.order_by().values(
            'pk', 'dc_capacity_kw', 'activated_at',
            design_status=F('design_assignment__status'),
            survey_link_added_at=F('design_assignment__survey_link_added_at'),
            survey_uploaded_at=F('design_assignment__survey_uploaded_at'),
            assigned_at=F('design_assignment__assigned_at'),
            released_at=F('design_assignment__released_at'),
            in_locked_group=Exists(locked),
            locked_at=Subquery(locked.values('group__locked_at')[:1]),
            qc_started_at=Subquery(current_attempt.values('qc_started_at')[:1]),
            # When the site last moved INTO its current status, from the ledger. Only
            # moves since 5 Sep 2026 (migration 0079) have a row; earlier ones read None.
            status_entered_at=latest_design_transition(
                'occurred_at', outer_ref='design_assignment__pk',
                to_status=OuterRef('design_assignment__status')),
        )
    )


def _stage_of(row):
    """(stage_key, entered_at) for one row of _site_rows(). Pure; no query."""
    # Activation beats every design and group stage: a site in execution is past design,
    # whatever the assignment still reads.
    if row['activated_at'] is not None:
        return STAGE_ACTIVATED, row['activated_at']
    # The furthest stage wins, so a locked group beats the design status even if the
    # design has been reopened since (decision 2a; the report counts any such site).
    if row['in_locked_group']:
        return STAGE_LOCKED_GROUP, row['locked_at']

    status = row['design_status']
    if status is None:
        # No DesignAssignment row: nothing has been uploaded for this site yet.
        return STAGE_NO_SURVEY, None
    stage = STATUS_TO_STAGE[status]

    if stage == STAGE_NO_SURVEY:
        # No reliable date: awaiting_survey has only the row's created_at, and a held site's
        # hold date is not read yet (SECONDARY_FINDINGS, S3).
        return stage, None
    if stage == STAGE_SURVEY_ON_FILE:
        # Either route satisfies the survey (DesignAssignment.survey_ready); the earlier one
        # is when the site became allocatable.
        dates = [d for d in (row['survey_link_added_at'], row['survey_uploaded_at']) if d]
        return stage, min(dates) if dates else None
    if stage == STAGE_IN_DESIGN:
        # FIRST entry: a return to S2 after a QC failure or PM send-back keeps this date.
        return stage, row['assigned_at']
    if stage == STAGE_IN_QC:
        # A PM rejection re-enters S3 from S4, and only the ledger records when.
        if status == DESIGN_PM_REJECTED:
            return stage, row['status_entered_at']
        return stage, row['qc_started_at']
    if stage == STAGE_AWAITING_PM:
        # No column on the assignment; the ledger is the only source.
        return stage, row['status_entered_at']
    return stage, row['released_at']      # STAGE_RELEASED


def site_stages(sites_qs):
    """{project_pk: StageInfo} for every site in `sites_qs`. One query, pure read.

    `sites_qs` is normally views.tender_sites_qs(); any Project queryset works.
    """
    result = {}
    for row in _site_rows(sites_qs):
        stage, entered_at = _stage_of(row)
        result[row['pk']] = StageInfo(stage, entered_at, row['design_status'])
    return result


def stage_summary(sites_qs):
    """One dict per stage, in STAGES order: key, label, sites, kwp, kwp_sites, held.

    kwp sums only the sites whose capacity is known, and kwp_sites says how many those
    are. A null OR zero dc_capacity_kw is unknown, matching views._tender_header, so the
    pipeline and the header can be read against each other. `held` counts Design Hold
    sites and can be non-zero only on S0. One query — the same one site_stages() runs.
    """
    summary = {key: {'key': key, 'label': label, 'sites': 0, 'kwp': Decimal('0'),
                     'kwp_sites': 0, 'held': 0}
               for key, label in STAGES}
    for row in _site_rows(sites_qs):
        stage, _ = _stage_of(row)
        entry = summary[stage]
        entry['sites'] += 1
        if row['dc_capacity_kw']:
            entry['kwp'] += row['dc_capacity_kw']
            entry['kwp_sites'] += 1
        if stage == STAGE_NO_SURVEY and row['design_status'] == DESIGN_SURVEY_RETURNED:
            entry['held'] += 1
    return [summary[key] for key, _ in STAGES]


def activated_progress(sites_qs):
    """How far the S7 (activated) sites have got, in one query:

        activated              S7 sites
        material_delivered     every applicable DELIVERY_* mirror task is Done
        installation_complete  every applicable task from the template's INSTALLATION
                               phase is Done
        commissioned           Project.status is Commissioned

    A site with NO task of a kind is not counted as complete for it: no tasks is no
    evidence. Two known gaps (SECONDARY_FINDINGS, S3): hand-added tasks with no
    template_task are invisible to the installation count, and a delivery mirror that is
    stale stays stale here, because there is no reconcile step behind it.
    """
    # The four mirror codes a delivery challan drives (sync_delivery_mirrors). Taken from
    # the mapping rather than restated, so a mirror added there is counted here.
    delivery_q = (Q(phases__tasks__is_mirror=True,
                    phases__tasks__template_task__code__in=set(DC_CATEGORY_TO_MIRROR_CODE.values()))
                  & applicable_tasks_q('phases__tasks__'))
    install_q = (Q(phases__tasks__template_task__phase__code=OPEX_INSTALLATION_PHASE_CODE)
                 & applicable_tasks_q('phases__tasks__'))
    done_q = Q(phases__tasks__status=Task.DONE)

    # All four counts ride ONE phases__tasks join; template_task and its phase are
    # single-valued FKs off Task, so the extra joins add no rows.
    rows = (sites_qs.filter(activated_at__isnull=False).order_by()
            .values('pk', 'status')
            .annotate(delivery_total=Count('phases__tasks', filter=delivery_q),
                      delivery_done=Count('phases__tasks', filter=delivery_q & done_q),
                      install_total=Count('phases__tasks', filter=install_q),
                      install_done=Count('phases__tasks', filter=install_q & done_q)))

    progress = {'activated': 0, 'material_delivered': 0,
                'installation_complete': 0, 'commissioned': 0}
    for row in rows:
        progress['activated'] += 1
        if row['delivery_total'] and row['delivery_done'] == row['delivery_total']:
            progress['material_delivered'] += 1
        if row['install_total'] and row['install_done'] == row['install_total']:
            progress['installation_complete'] += 1
        if row['status'] == 'Commissioned':
            progress['commissioned'] += 1
    return progress


# The five buckets a tender card folds S0-S7 into, in workflow order. The stacked bar and
# its legend render in this order, light to dark, so survey is always on the left.
BUCKETS = [
    ('survey',      'Survey',      (STAGE_NO_SURVEY, STAGE_SURVEY_ON_FILE)),
    ('design',      'Design',      (STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_AWAITING_PM)),
    ('released',    'Released',    (STAGE_RELEASED,)),
    ('procurement', 'Procurement', (STAGE_LOCKED_GROUP,)),
    ('execution',   'Execution',   (STAGE_ACTIVATED,)),
]

# Worst first: the precedence _get_ceo_dashboard_context gives a project card's badge
# (Blocked > Delayed > At Risk > On Time), so a tender's pill agrees with its sites' pills.
HEALTH_SEVERITY = ['blocked', 'delayed', 'at_risk', 'on_time']


def tender_cards(sites_qs, health_by_pk):
    """One card per programme with at least one site in `sites_qs`, sites descending then
    name. TWO queries whatever the number of programmes or sites: site_stages() and one
    values() read of the programme and display columns, joined here by pk.

    Each card: programme pk, name, code, detail url; sites, kwp, kwp_sites; `buckets` (the
    five BUCKETS with sites and pct of the tender's sites); released, released_kwp,
    released_kwp_sites; `stages` (S0-S7 with sites, kwp, kwp_sites); `activated` (S7 sites
    with pk, project_id, name, badge, status); `health` (the worst activated badge).

    `health_by_pk` is {project pk: badge} from the dashboard's own project cards, passed
    in rather than recomputed: the badge rule lives in views, which imports this module,
    so reading it here would be a cycle and restating it would be a second rule. A site
    that is not in it (On Hold, Commissioned — outside projects_qs) has badge None; the
    template shows its status instead, and it takes no part in the tender's health.

    RELEASED IS THE DESIGN STATUS, NOT THE S5 COUNT. A site released and then locked into
    a group, or activated, has moved past S5 but its design is still released, and the
    card's "Design released" counts it. An activated site whose design is still open is
    in S7 and is NOT counted.

    A site with no programme belongs to no tender and has no card (SECONDARY_FINDINGS, S4).
    kWp counts only known capacity — null or zero is unknown — as stage_summary() does.
    """
    stages = site_stages(sites_qs)
    rows = sites_qs.filter(program__isnull=False).order_by().values(
        'pk', 'program_id', 'project_id', 'site_name', 'customer_name', 'status',
        'dc_capacity_kw',
        program_name=F('program__name'), program_code=F('program__short_tender_code'),
    )
    stage_number = {key: n for n, (key, _) in enumerate(STAGES)}
    bucket_of = {stage: key for key, _, members in BUCKETS for stage in members}

    cards = {}
    for row in rows:
        card = cards.get(row['program_id'])
        if card is None:
            card = cards[row['program_id']] = {
                'program_pk': row['program_id'],
                'name': row['program_name'],
                'code': row['program_code'],
                'url': reverse('program_detail', args=[row['program_id']]),
                'sites': 0, 'kwp': Decimal('0'), 'kwp_sites': 0,
                'buckets': {key: {'key': key, 'label': label, 'sites': 0}
                            for key, label, _ in BUCKETS},
                'released': 0, 'released_kwp': Decimal('0'), 'released_kwp_sites': 0,
                'stages': [{'code': f'S{n}', 'key': key, 'label': label, 'sites': 0,
                            'kwp': Decimal('0'), 'kwp_sites': 0}
                           for n, (key, label) in enumerate(STAGES)],
                'activated': [],
            }
        info = stages[row['pk']]
        kwp = row['dc_capacity_kw']      # falsy for null and zero: both unknown
        stage = card['stages'][stage_number[info.stage_key]]

        card['sites'] += 1
        stage['sites'] += 1
        card['buckets'][bucket_of[info.stage_key]]['sites'] += 1
        if kwp:
            card['kwp'] += kwp
            card['kwp_sites'] += 1
            stage['kwp'] += kwp
            stage['kwp_sites'] += 1
        if info.design_status == DESIGN_RELEASED:
            card['released'] += 1
            if kwp:
                card['released_kwp'] += kwp
                card['released_kwp_sites'] += 1
        if info.stage_key == STAGE_ACTIVATED:
            card['activated'].append({
                'pk': row['pk'],
                'project_id': row['project_id'],
                'name': row['site_name'] or row['customer_name'],
                'badge': health_by_pk.get(row['pk']),
                'status': row['status'],
            })

    for card in cards.values():
        buckets = [card['buckets'][key] for key, _, _ in BUCKETS]
        for bucket in buckets:
            bucket['pct'] = round(bucket['sites'] * 100 / card['sites'], 1)
        card['buckets'] = buckets
        card['activated'].sort(key=lambda site: site['project_id'])
        badges = [site['badge'] for site in card['activated'] if site['badge']]
        card['health'] = min(badges, key=HEALTH_SEVERITY.index) if badges else None

    return sorted(cards.values(), key=lambda card: (-card['sites'], card['name']))


def design_throughput(sites_qs, today):
    """Whether design is moving, for the CEO Tenders "Design throughput" card (S5). FOUR
    queries whatever the number of sites or programmes: assignments, attempts, due-date
    commitments and open change requests, each scoped by `sites_qs` as a subquery.

    EVERY FIGURE IS THE DESIGN HEAD'S OWN (S5 hard rule). Nothing below restates a
    definition; the rows are shaped the way tender_metrics() / analytics_dataset() shape
    them and handed to the same functions:

        released_week / released_month
                        status == released AND released_at's IST date inside the calendar
                        week (Mon-Sun) / month containing `today`. The Head has no windowed
                        figure; "released" is his strict one (awaiting PM is not released).
                        Accepting a change request clears released_at, so these can fall
                        after the fact (SECONDARY_FINDINGS, S5).
        cycle           design_analytics.m_cycle_time()'s team figure, all time: working
                        days from allocation to release, median and n. `cycle_insufficient`
                        is its own MIN_DENOMINATOR state, so the card says "too few to
                        judge" exactly when the Head's analytics does.
        rework_loops / finished_sites
                        design_metrics.rework_contribution() summed: designer-caused
                        attempts on finished sites, and the finished sites. The numerator
                        of the Head's Rework column and its denominator. A change request
                        opens a pm_change attempt, which that function never counts.
        change_requests open = CHANGE_REQUEST_OPEN_VERDICTS (with the PM, or pending with
                        the Head), split by origin and by where it waits. `with_head` is
                        the count the Head's own change-request queue shows.
        on_hold         status == survey_returned, the Head's `blocked`.
        past_due        design_metrics.is_overdue() on effective_commitment(), no stage
                        filter: a held site past its date counts, a PM-rejected one does
                        not, exactly as on the Head's dashboard.

    `today` is passed in (the caller's timezone.localdate()) so the windows and the overdue
    test share one IST date.
    """
    # The site pks as a subquery, so every read below is one statement with no pk list.
    site_pks = sites_qs.order_by().values('pk')

    # assigned_to__user rides along because m_cycle_time labels its per-designer rows
    # (unused here) by the designer's name; without it each released site costs a query.
    assignments = list(DesignAssignment.objects.filter(project__in=site_pks)
                       .select_related('assigned_to__user'))
    attempts_by_assignment = {}
    for attempt in DesignAttempt.objects.filter(assignment__project__in=site_pks):
        attempts_by_assignment.setdefault(attempt.assignment_id, []).append(attempt)
    commitments_by_assignment = {}
    for commitment in DueDateCommitment.objects.filter(assignment__project__in=site_pks):
        commitments_by_assignment.setdefault(commitment.assignment_id, []).append(commitment)
    # Open requests only: the card counts what is waiting, and closed verdicts are history.
    open_requests = list(DesignChangeRequest.objects
                         .filter(attempt__assignment__project__in=site_pks,
                                 verdict__in=CHANGE_REQUEST_OPEN_VERDICTS)
                         .values_list('origin', 'verdict'))

    # The same per-site keys tender_metrics() and analytics_dataset() build, from the same
    # status sets, so the shared functions read them exactly as they read the Head's.
    sites = [{
        'assignment': a,
        'designer':   a.assigned_to,
        'attempts':   attempts_by_assignment.get(a.pk, []),
        'released':   a.status == DESIGN_RELEASED,
        'finished':   a.status in DESIGN_WORK_FINISHED_STATUSES,
    } for a in assignments]

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    released_week = released_month = 0
    for s in sites:
        released_at = s['assignment'].released_at
        if not s['released'] or released_at is None:
            continue
        # The IST calendar date of the release, so 00:30 on a Monday is the new week.
        released_on = timezone.localtime(released_at).date()
        if week_start <= released_on < week_end:
            released_week += 1
        if (released_on.year, released_on.month) == (today.year, today.month):
            released_month += 1

    cycle = m_cycle_time({'sites': sites})['team']

    rework_loops = finished_sites = 0
    for s in sites:
        contribution = rework_contribution(s)
        rework_loops += contribution['designer']
        finished_sites += contribution['finished']

    past_due = sum(
        1 for s in sites
        if is_overdue(s['assignment'],
                      effective_commitment(commitments_by_assignment.get(s['assignment'].pk, [])),
                      today))

    return {
        'week_start': week_start,
        'released_week': released_week,
        'released_month': released_month,
        'cycle': cycle,
        'cycle_insufficient': cycle['state'] == STATE_INSUFFICIENT,
        'rework_loops': rework_loops,
        'finished_sites': finished_sites,
        'change_requests': {
            'open': len(open_requests),
            'pm': sum(1 for origin, _ in open_requests if origin != CHANGE_REQUEST_ORIGIN_SCM),
            'scm': sum(1 for origin, _ in open_requests if origin == CHANGE_REQUEST_ORIGIN_SCM),
            'with_pm': sum(1 for _, verdict in open_requests
                           if verdict == CHANGE_REQUEST_WITH_PM),
            # The only other open verdict is `pending`: with the Design Head.
            'with_head': sum(1 for _, verdict in open_requests
                             if verdict != CHANGE_REQUEST_WITH_PM),
        },
        'on_hold': sum(1 for s in sites if s['assignment'].status == DESIGN_SURVEY_RETURNED),
        'past_due': past_due,
    }
