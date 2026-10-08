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

stuck_sites() (S7) follows S5's rule: where the app already defines "late" for a stage it
calls that definition, and STUCK_LIMITS holds the few limits nothing else had. Seven
queries whatever the number of sites.

tender_site_list() (S9) is the CEO's click-through list behind every count above. It runs
stuck_sites()'s seven reads and its per-site verdict (_stuck_entry) rather than a filter of
its own, so a list holds exactly the sites its count says and a stuck site's "Waiting on"
and days read as they do in the stuck section.
"""
from collections import namedtuple
from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    BooleanField, Count, Exists, ExpressionWrapper, F, OuterRef, Q, Subquery,
)
from django.urls import reverse
from django.utils import timezone

from .design_analytics import STATE_INSUFFICIENT, m_cycle_time
# _classify is design_metrics' private court split (whose turn a site is). stuck_sites()
# imports it rather than restating it, so "waiting on" matches the Head's own screens.
from .design_metrics import (
    HEAD_ACTION_STAGES, QC_ACTION_STAGES, _classify, days_overdue, effective_commitment,
    is_overdue, rework_contribution,
)
# latest_design_transition lives in design_views beside the ledger's other readers.
# design_views does not import views or this module, so importing it here is not a cycle.
from .design_views import latest_design_transition
from .models import (
    CHANGE_REQUEST_OPEN_VERDICTS, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM,
    DC_CATEGORY_TO_MIRROR_CODE, DESIGN_ASSIGNMENT_STATUS_CHOICES,
    DESIGN_ALLOCATED, DESIGN_ARKA_REJECTED, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_HEAD_ARKA,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_AWAITING_SURVEY,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED,
    DESIGN_QC_FAILED, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    DESIGN_WORK_FINISHED_STATUSES,
    GROUP_TYPE_PROCUREMENT, SITE_GROUP_LOCKED,
    ArkaSubmission, CodRecord, DesignAssignment, DesignAttempt, DesignChangeRequest, DueDateCommitment,
    SiteGroupMembership, Task, UserProfile, VendorOrderSite,
)
# The TASK overdue rule, for the S7 stuck term. Imported as a module because is_overdue /
# days_overdue above are design_metrics' and mean a design due date, not a task's.
from . import task_health
from .utils import applicable_tasks_q, human_owned_tasks_q


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


def has_active_cod(outer_ref='pk'):
    """Exists(): the site at `outer_ref` has an active (not withdrawn) CodRecord.

    THE ONE DEFINITION of "COD on record" for a queryset of sites. An expression, not a
    filter, so the pipeline and the stuck list carry it as a column on the query they
    already run and spend no query of their own. Active is `withdrawn_at IS NULL`, the
    condition of the model's uniq_cod_record_active constraint.

    WHY THE RECORD AND NOT Project.status (closeout 4b go-ahead): Commissioned already
    means "work over" to ~30 readers that drop it from working lists, while a site with a
    COD still has HOTO, As-Built and Final Acceptance open. So COD leaves the status
    alone and is read from the record; status Commissioned moves to HOTO (step 8, G59).
    """
    return Exists(CodRecord.objects.filter(project=OuterRef(outer_ref),
                                           withdrawn_at__isnull=True))


def activated_progress(sites_qs):
    """How far the S7 (activated) sites have got, in one query:

        activated              S7 sites
        material_delivered     every applicable DELIVERY_* mirror task is Done
        installation_complete  every applicable task from the template's INSTALLATION
                               phase is Done
        commissioned           an active COD record (has_active_cod), or Project.status
                               Commissioned — no writer sets that status yet; step 8
                               (HOTO) decides whether one will (G59)

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
    # single-valued FKs off Task, so the extra joins add no rows. has_cod is a correlated
    # EXISTS on the same row, so it adds a column, not a query.
    rows = (sites_qs.filter(activated_at__isnull=False).order_by()
            .values('pk', 'status', has_cod=has_active_cod())
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
        if row['has_cod'] or row['status'] == 'Commissioned':
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


# ---------------------------------------------------------------------------
# S7 — stuck sites
# ---------------------------------------------------------------------------

# THE ONLY PLACE THE CEO VIEW'S OWN LIMITS LIVE (S7 hard rule). An entry whose `rule`
# names a function reuses that definition and carries no number of its own; a number here
# is a limit set because nothing in the app defined "late" for that stage (pre-flight P1,
# rulings 1-3). Compared >= (ruling 4): a site is stuck ON the day it reaches its limit,
# and its overshoot is days - limit. S0 has no entry: a site with no survey is never
# stuck (D6).
STUCK_LIMITS = {
    STAGE_SURVEY_ON_FILE: {'days': 7, 'rule': None},
    STAGE_IN_DESIGN:      {'days': None, 'rule': 'design_metrics.is_overdue'},
    # is_overdue() never counts a PM-rejected package (the designer delivered), so that
    # one status gets our own limit instead, dated from the ledger row of the rejection.
    STAGE_IN_QC:          {'days': None, 'rule': 'design_metrics.is_overdue',
                           'pm_rejected_days': 7},
    STAGE_AWAITING_PM:    {'days': 3, 'rule': None},
    STAGE_RELEASED:       {'days': 7, 'rule': None},
    # Stuck only while no PO/PI is recorded for the site (no VendorOrderSite row).
    STAGE_LOCKED_GROUP:   {'days': 7, 'rule': None},
    # The CEO page's own Blocked Tasks "Aged >= 7 days" and Tasks "Overdue" terms. The
    # 7 days arrive as the caller's aged_block_cutoff, never as a number here.
    STAGE_ACTIVATED:      {'days': None,
                           'rule': 'views._get_ceo_dashboard_context blocked_aged_7d / task_overdue'},
}

# The summary line's short stage names, in STAGES order.
STUCK_SUMMARY_LABELS = {
    STAGE_SURVEY_ON_FILE: 'Design backlog',
    STAGE_IN_DESIGN:      'In design',
    STAGE_IN_QC:          'In QC',
    STAGE_AWAITING_PM:    'Awaiting PM',
    STAGE_RELEASED:       'Released, not grouped',
    STAGE_LOCKED_GROUP:   'Locked, no PO/PI',
    STAGE_ACTIVATED:      'In execution',
}

# Sites sharing tender, stage, rule and clock date fold into one row from this many up
# (the S7 prompt's rule). A display rule, not a limit, so it is not in STUCK_LIMITS.
STUCK_GROUP_MIN = 5

# The Blocked Tasks card counts tasks on Active and In Progress projects only (its
# `active_statuses`); an S7 site On Hold or Commissioned is not stuck (ruling 3). Nor is
# one with a COD on record (has_active_cod, closeout 4b): see _stuck_entry.
STUCK_EXECUTION_STATUSES = ['Active', 'In Progress']

# Every `rule` a stuck entry can carry, and the word the S9 list's filter chip puts before
# its clock date. The rule is part of the grouping key, so a grouped row's list link names
# it; the list 404s on any value not here.
STUCK_RULES = {
    'limit':        'In stage since',        # S1, S4, S5, S6: our limit from entered_at
    'due':          'Due',                   # S2, S3: design_metrics.is_overdue
    'pm_rejected':  'PM rejection on',       # S3 sent back by the PM: our limit
    'blocked':      'Task blocked since',    # S7: Blocked Tasks "Aged" term
    'overdue_task': 'Task due',              # S7: Tasks "Overdue" term
}


def _days_text(n):
    return f'{n} day' if n == 1 else f'{n} days'


def _due_text(on):
    # Day first, no leading zero, without the platform-specific %-d.
    return f'Due {on.day} {on:%b}'


def stuck_limit_lines(block_days):
    """The page's footer: one line per stage S1-S7, always all seven (ruling 7), built from
    STUCK_LIMITS so the footer cannot name a limit the rows do not use. `block_days` is the
    Blocked Tasks card's own age limit, read off its cutoff by stuck_sites()."""
    L = STUCK_LIMITS
    return [
        f"Survey on file → allocated: {_days_text(L[STAGE_SURVEY_ON_FILE]['days'])} (our limit)",
        "In design: past agreed due date (Design Head's rule)",
        (f"In QC / Head QC: past agreed due date (Design Head's rule); sent back by the "
         f"PM: {_days_text(L[STAGE_IN_QC]['pm_rejected_days'])} (our limit)"),
        f"Awaiting PM approval: {_days_text(L[STAGE_AWAITING_PM]['days'])} (our limit)",
        (f"Released → in a locked procurement group: "
         f"{_days_text(L[STAGE_RELEASED]['days'])} (our limit)"),
        f"Locked group → PO/PI recorded: {_days_text(L[STAGE_LOCKED_GROUP]['days'])} (our limit)",
        (f"In execution: a task blocked {_days_text(block_days)} or more, or an internal "
         f"task past its due date (CEO Blocked Tasks and Tasks cards)"),
    ]


def _person(profile):
    return profile.user.get_full_name() or profile.user.username


def _one(people, pk, role, empty):
    """'Name · Role' when the record names someone we read, else `empty` (S8 T1)."""
    person = people.get(pk) if pk else None
    return f'{_person(person)} · {role}' if person else empty


def _local_date(dt):
    return timezone.localtime(dt).date()


# _stuck_entry()'s answer for a site whose clock cannot start: no stage entry date.
_UNDATED = object()


def _stuck_reads(sites_qs, today, aged_block_cutoff):
    """stuck_sites()'s SEVEN reads, returned as one dict so the S9 list can run the same
    reads and the same per-site verdict (split out in S9, behaviour unchanged; the S7
    fixtures and a golden copy of their output in tests_ceo_tender_sites pin it).

    Keys: stages (site_stages), site_rows, assignments, commitments, arkas, tasks_by_site,
    task_counts, people, block_days. The query budget is stuck_sites()'s docstring's.
    """
    stages = site_stages(sites_qs)                                           # query 1
    site_pks = sites_qs.order_by().values('pk')

    # Same three terms as _site_rows()'s `locked` (and project_boq_is_group_locked):
    # a live, procurement-typed membership of a locked group.
    locked = SiteGroupMembership.objects.filter(
        project=OuterRef('pk'), removed_at__isnull=True,
        group_type=GROUP_TYPE_PROCUREMENT, group__status=SITE_GROUP_LOCKED,
    )
    # One row per site with every id "waiting on" can name; the names come from query 7.
    # The S6 owner follows design_views._change_request_group_owner (D6): whoever added
    # the site if active, else whoever created the group if active. dc_capacity_kw rides
    # along for the S9 list's Capacity column; stuck_sites() does not read it.
    site_rows = {row['pk']: row for row in sites_qs.order_by().values(      # query 2
        'pk', 'project_id', 'site_name', 'status', 'program_id', 'assigned_pm_id',
        'dc_capacity_kw',
        program_name=F('program__name'),
        designer_pk=F('design_assignment__assigned_to'),
        # in_qc stores no reviewer; the ledger row INTO in_qc names who started QC.
        qc_actor_pk=latest_design_transition(
            'actor', outer_ref='design_assignment__pk', to_status=DESIGN_IN_QC),
        group_adder_pk=Subquery(
            locked.filter(added_by__is_active=True, added_by__user__is_active=True)
            .values('added_by')[:1]),
        group_creator_pk=Subquery(
            locked.filter(group__created_by__is_active=True,
                          group__created_by__user__is_active=True)
            .values('group__created_by')[:1]),
        has_order=Exists(VendorOrderSite.objects.filter(project=OuterRef('pk'))),
        # Read by _stuck_entry's S7 branch: a site with a COD on record is not stuck.
        has_cod=has_active_cod(),
    )}

    # The Head's three reads (tender_metrics()), scoped by the site subquery. EVERY
    # assignment, not only S2/S3: the no-due-date count covers every unfinished site.
    assignments = {a.project_id: a for a in                                  # query 3
                   DesignAssignment.objects.filter(project__in=site_pks)}
    commitments = {}
    for c in DueDateCommitment.objects.filter(assignment__project__in=site_pks):  # query 4
        commitments.setdefault(c.assignment_id, []).append(c)
    # The current Arka of the CURRENT attempt, matched on the assignment's pointer as
    # tender_metrics() does; _classify() needs it to split arka_submitted by court.
    arkas = {(k.attempt.assignment_id, k.attempt.attempt_number): k for k in   # query 5
             ArkaSubmission.objects.filter(attempt__assignment__project__in=site_pks,
                                           is_current=True).select_related('attempt')}

    # S7: the page's two task terms, on the same human-owned, applicable base as its
    # task aggregate. "Overdue" is the shared rule (task_health) scoped to Internal, as
    # on the CEO Tasks card.
    #
    # ONE TASK CAN NOW MATCH BOTH. The shared rule counts a Blocked task that is past its
    # date, so a task blocked 7 days or more AND past due is in both counts, as it is on
    # both cards. Each row therefore carries the two answers (is_aged, is_late), computed
    # in SQL from the very Q objects that filter it, instead of being sorted into one
    # branch by its status.
    execution_pks = sites_qs.filter(activated_at__isnull=False,
                                    status__in=STUCK_EXECUTION_STATUSES).order_by().values('pk')
    aged_q = Q(status=Task.BLOCKED, blocked_since__lte=aged_block_cutoff,
               blocked_since__isnull=False)
    late_q = Q(task_type=Task.INTERNAL) & task_health.overdue_q(today)
    tasks_by_site = {}
    task_counts = {'blocked_aged': 0, 'overdue': 0}
    for t in (Task.objects.filter(phase__project__in=execution_pks)          # query 6
              .filter(human_owned_tasks_q()).filter(applicable_tasks_q())
              .filter(aged_q | late_q)
              .values('pk', 'task_name', 'status', 'blocked_since', 'due_date',
                      'assigned_to_id', project_pk=F('phase__project_id'),
                      is_aged=ExpressionWrapper(aged_q, output_field=BooleanField()),
                      is_late=ExpressionWrapper(late_q, output_field=BooleanField()))
              .order_by('pk')):
        tasks_by_site.setdefault(t['project_pk'], []).append(t)
        task_counts['blocked_aged'] += bool(t['is_aged'])
        task_counts['overdue'] += bool(t['is_late'])

    # Everyone a row can name, in ONE read. Role holders are not read: a stage the record
    # pins on nobody shows the role alone (S8 T1), whoever holds it.
    ids = {pk for row in site_rows.values()
           for pk in (row['assigned_pm_id'], row['designer_pk'], row['qc_actor_pk'],
                      row['group_adder_pk'], row['group_creator_pk']) if pk}
    ids |= {t['assigned_to_id'] for ts in tasks_by_site.values() for t in ts
            if t['assigned_to_id']}
    people = {p.pk: p for p in UserProfile.objects.filter(                   # query 7
        pk__in=ids).select_related('user').order_by('pk')}

    return {
        'stages': stages, 'site_rows': site_rows, 'assignments': assignments,
        'commitments': commitments, 'arkas': arkas, 'tasks_by_site': tasks_by_site,
        'task_counts': task_counts, 'people': people,
        # The card's 7 days, read back off its cutoff, so the number is never restated here.
        'block_days': (today - _local_date(aged_block_cutoff)).days,
    }


def _waiting_on(reads, pk, stage):
    """Who holds an S1-S6 site's next step, as the stuck section words it. Pure; no query.

    S7 is not here: a stuck S7 site's text names its worst task, which only _stuck_entry()
    has picked. The S9 list calls this for S1-S6 sites that are not stuck, so a site reads
    the same whether or not it has passed its limit.
    """
    row, people = reads['site_rows'][pk], reads['people']
    if stage in (STAGE_IN_DESIGN, STAGE_IN_QC):
        a = reads['assignments'][pk]
        if a.status == DESIGN_PM_REJECTED:
            return 'Design Head'
        court = _classify(a, reads['arkas'].get((a.pk, a.current_attempt_number)))
        if court in HEAD_ACTION_STAGES:
            return 'Design Head'
        if court in QC_ACTION_STAGES:
            # Started QC names its reviewer; a package not yet picked up is the queue's.
            return (_one(people, row['qc_actor_pk'], 'Design QC', 'Design QC')
                    if a.status == DESIGN_IN_QC else 'Design QC')
        return _one(people, row['designer_pk'], 'Designer', 'No designer allocated')
    if stage == STAGE_SURVEY_ON_FILE:
        return 'Design Head'
    if stage == STAGE_AWAITING_PM:
        return _one(people, row['assigned_pm_id'], 'PM', 'No PM assigned')
    if stage == STAGE_LOCKED_GROUP:
        return _one(people, row['group_adder_pk'] or row['group_creator_pk'], 'SCM', 'SCM')
    return 'SCM'           # S5: no group yet, so no owner to name


def _stuck_entry(reads, pk, info, today):
    """One site's stuck verdict: its entry dict when it is past its stage's limit, None
    when it is not (or cannot be: S0), _UNDATED when its clock has no start. Pure; no
    query. stuck_sites() folds these into rows; the S9 list shows them per site."""
    stage, row = info.stage_key, reads['site_rows'][pk]
    people, block_days = reads['people'], reads['block_days']

    def _entry(rule, clock_date, days, overshoot, days_text, limit_text, waiting_on):
        label = row['project_id']
        if row['site_name']:
            label = f"{label} · {row['site_name']}"
        return {
            'program_pk': row['program_id'], 'tender': row['program_name'] or '—',
            'site': label, 'sites': 1, 'stage': stage, 'stage_label': STAGE_LABELS[stage],
            'rule': rule, 'clock_date': clock_date, 'days': days, 'overshoot': overshoot,
            'days_text': days_text, 'limit_text': limit_text, 'waiting_on': waiting_on,
            'url': reverse('project_overview', args=[row['project_id']]),
        }

    if stage == STAGE_NO_SURVEY:
        return None

    if stage in (STAGE_IN_DESIGN, STAGE_IN_QC):
        a = reads['assignments'][pk]
        if a.status == DESIGN_PM_REJECTED:
            if info.entered_at is None:
                return _UNDATED
            limit = STUCK_LIMITS[STAGE_IN_QC]['pm_rejected_days']
            days = (today - _local_date(info.entered_at)).days
            if days < limit:
                return None
            return _entry('pm_rejected', _local_date(info.entered_at), days, days - limit,
                          f'{_days_text(days)} since PM rejection', _days_text(limit),
                          _waiting_on(reads, pk, stage))
        commitment = effective_commitment(reads['commitments'].get(a.pk, []))
        if not is_overdue(a, commitment, today):
            return None
        over = days_overdue(commitment, today)
        return _entry('due', commitment.proposed_date, over, over,
                      f'{_days_text(over)} past due', _due_text(commitment.proposed_date),
                      _waiting_on(reads, pk, stage))

    if stage == STAGE_ACTIVATED:
        if row['status'] not in STUCK_EXECUTION_STATUSES:
            return None
        # A COD on record ends "in execution" for the stuck list (closeout 4b), though
        # the status stays Active. Skipped here rather than dropped from query 6, so
        # task_counts still equal the CEO Blocked Tasks and Tasks cards, which keep
        # counting the site's open HOTO / As-Built work.
        if row['has_cod']:
            return None
        tasks = reads['tasks_by_site'].get(pk, [])
        if not tasks:
            return None
        candidates = []
        for t in tasks:
            # One candidate per TASK, so "(+N more)" counts tasks. A task that is both
            # blocked past the limit and past its due date enters once, under whichever
            # of its two rules overshoots further (blocked wins a tie, as between tasks).
            options = []
            if t['is_aged']:
                since = _local_date(t['blocked_since'])
                days = (today - since).days
                options.append((days - block_days, 1, 'blocked', since, days, t))
            if t['is_late']:
                days = (today - t['due_date']).days
                options.append((days, 0, 'overdue_task', t['due_date'], days, t))
            candidates.append(max(options, key=lambda c: (c[0], c[1])))
        # Worst overshoot; a blocked task beats an overdue one at equal overshoot; then
        # the lower task pk, so the row never changes between loads.
        over, _, rule, clock, days, task = max(candidates,
                                                key=lambda c: (c[0], c[1], -c[5]['pk']))
        more = len(candidates) - 1
        what = 'blocked' if rule == 'blocked' else 'overdue'
        waiting = (f"{_one(people, row['assigned_pm_id'], 'PM', 'No PM assigned')} — {what}: "
                   f"{task['task_name']}" + (f' (+{more} more)' if more else ''))
        if rule == 'blocked':
            return _entry(rule, clock, days, over, f'{_days_text(days)} blocked',
                          f'Blocked {_days_text(block_days)}', waiting)
        return _entry(rule, clock, days, over, f'{_days_text(days)} past due',
                      _due_text(clock), waiting)

    # S1, S4, S5, S6: our limit from the stage's entry date.
    if stage == STAGE_LOCKED_GROUP and row['has_order']:
        return None
    if info.entered_at is None:
        return _UNDATED
    limit = STUCK_LIMITS[stage]['days']
    days = (today - _local_date(info.entered_at)).days
    if days < limit:
        return None
    return _entry('limit', _local_date(info.entered_at), days, days - limit,
                  _days_text(days), _days_text(limit), _waiting_on(reads, pk, stage))


def stuck_sites(sites_qs, today, aged_block_cutoff):
    """Which live tender sites are stuck, where, for how long and who holds them (S7).

    SEVEN QUERIES whatever the number of sites (six when no row names anyone, since
    Django skips an IN () read), all in _stuck_reads(): site_stages(); one values() read
    of the display and "waiting on" columns; the design assignments, their due-date
    commitments and their current Arkas (the Head's own three reads, scoped by
    `sites_qs`); the S7 tasks; and one read of every person named. tests_stuck_sites pins
    3 and 30 sites.

    WAITING ON names a person only when the record names one — the designer, the QC
    reviewer from the ledger, the PM, the group's adder or creator — as "Name · Role".
    A stage owned by a role rather than a person (the Head's backlog, the QC queue, an
    in_qc package with no ledger row) shows the role alone (S8 T1): listing every holder
    of the role read as if all of them were holding the site.

    THE RULE PER STAGE (STUCK_LIMITS; every limit >= and overshoot = days - limit):
      S1, S4, S5, S6  our limit, counted in IST calendar days from the stage's entered_at.
                      S6 counts only while the site has no PO/PI record. A site whose
                      entry date is unknown (S4 before the ledger) cannot be timed and is
                      counted in `undated` instead of being guessed at.
      S2, S3          design_metrics.is_overdue() on effective_commitment(), exactly as
                      the Head's dashboard: a pending extension still counts, a site with
                      no approved date does not. Overshoot is days_overdue(). A
                      PM-rejected package (never overdue there) takes our S3 limit from
                      the ledger date of the rejection, waiting on the Design Head.
      S7              the CEO page's Blocked Tasks "Aged >= 7 days" and Tasks "Overdue"
                      terms, restated on the same human-owned, applicable tasks, with the
                      page's own `aged_block_cutoff` passed in. Active / In Progress only,
                      and never a site with a COD on record (has_active_cod).
                      "Overdue" is task_health.overdue_q() scoped to Internal, so a
                      Blocked task past its due date is late too.
      S0              never.

    Rows: longest overshoot first, then tender name, then site label. Five or more sites
    with the same tender, stage, rule and clock date (entered_at for a limit, the due date
    for a due-date rule) fold into one "N sites" row linked to the programme page.

    `no_due_date` is, per tender, the Head's own count: unfinished sites with no approved
    due date (tender_metrics()'s expression, on this scope). `task_counts` holds the S7
    task totals so tests can hold them equal to the page's two cards.
    """
    reads = _stuck_reads(sites_qs, today, aged_block_cutoff)
    site_rows, assignments, commitments = (
        reads['site_rows'], reads['assignments'], reads['commitments'])
    block_days, task_counts = reads['block_days'], reads['task_counts']

    entries = []
    undated = 0
    for pk, info in reads['stages'].items():
        entry = _stuck_entry(reads, pk, info, today)
        if entry is _UNDATED:
            undated += 1
        elif entry is not None:
            entries.append(entry)

    # Fold 5+ sites with one tender, stage, rule and clock date into one row. The clock
    # date fixes the overshoot, so every site in a group shares the row's numbers.
    buckets = {}
    for e in entries:
        if e['program_pk'] is not None:
            buckets.setdefault((e['program_pk'], e['stage'], e['rule'], e['clock_date']),
                               []).append(e)
    rows = [e for e in entries if e['program_pk'] is None]
    for members in buckets.values():
        if len(members) < STUCK_GROUP_MIN:
            rows.extend(members)
            continue
        first = members[0]
        waiting = {m['waiting_on'] for m in members}
        rows.append({**first,
                     'site': f'{len(members)} sites', 'sites': len(members),
                     'waiting_on': (first['waiting_on'] if len(waiting) == 1
                                    else f'{len(waiting)} people'),
                     'url': reverse('program_detail', args=[first['program_pk']])})
    rows.sort(key=lambda r: (-r['overshoot'], r['tender'], r['site']))

    by_stage = {key: 0 for key in STUCK_SUMMARY_LABELS}
    for e in entries:
        by_stage[e['stage']] += 1

    # The Head's no_due_date, per tender, on this scope: tender_metrics() counts every
    # assignment that is not finished and has no approved effective commitment.
    no_due = {}
    for project_pk, a in assignments.items():
        row = site_rows[project_pk]
        if row['program_id'] is None:
            continue          # the Head's count is per programme; no programme, no count
        commitment = effective_commitment(commitments.get(a.pk, []))
        if (not bool(commitment and commitment.approved_at)
                and a.status not in DESIGN_WORK_FINISHED_STATUSES):
            no_due[row['program_name']] = no_due.get(row['program_name'], 0) + 1

    return {
        'rows': rows,
        'stuck': len(entries),
        'by_stage': [{'key': key, 'label': STUCK_SUMMARY_LABELS[key], 'sites': by_stage[key]}
                     for key, _ in STAGES if key in STUCK_SUMMARY_LABELS and by_stage[key]],
        'no_due_date': [{'tender': name, 'count': n} for name, n in sorted(no_due.items())],
        'undated': undated,
        'limits': stuck_limit_lines(block_days),
        'task_counts': task_counts,
    }


# ---------------------------------------------------------------------------
# S9 — the click-through site list
# ---------------------------------------------------------------------------

DESIGN_STATUS_LABELS = dict(DESIGN_ASSIGNMENT_STATUS_CHOICES)


def tender_site_list(sites_qs, today, aged_block_cutoff):
    """Every site in `sites_qs` as one row of the CEO's click-through list (S9), in the
    list's default order. stuck_sites()'s SEVEN reads (six when nobody is named) whatever
    the number of sites, and no other query; the caller filters in Python, so a list is
    always a subset of exactly the rows the dashboard counted.

    Each row: pk, project_id, site ("ID · site name"), url, program_pk, tender, stage,
    stage_code, stage_label, design_status (display label, '—' with no assignment),
    days (IST days since entered_at, None when undated), in_stage, dc_capacity_kw,
    waiting_on, stuck, and the stuck entry's rule, clock_date and overshoot (None when
    not stuck).

    A STUCK site's in_stage and waiting_on are its _stuck_entry() texts, the very strings
    the stuck section shows. Any other site reads its days in stage ('—' when undated) and
    _waiting_on(), so a site's holder does not change the day it passes its limit. No one
    is named for S0 (no survey is never Design's, D6), for an S6 site whose PO/PI is
    recorded, or for an S7 site with nothing stuck (S9 ruling 3).

    Order: stuck first, by the stuck section's key (longest overshoot, tender, site); then
    the rest by days in stage, longest first, undated last; then project_id.
    """
    reads = _stuck_reads(sites_qs, today, aged_block_cutoff)
    stage_number = {key: n for n, (key, _) in enumerate(STAGES)}
    stuck_rows, other_rows = [], []
    for pk, info in reads['stages'].items():
        row, stage = reads['site_rows'][pk], info.stage_key
        entry = _stuck_entry(reads, pk, info, today)
        if entry is _UNDATED:
            entry = None
        days = (today - _local_date(info.entered_at)).days if info.entered_at else None

        if entry:
            in_stage, waiting = entry['days_text'], entry['waiting_on']
        else:
            in_stage = _days_text(days) if days is not None else '—'
            if (stage in (STAGE_NO_SURVEY, STAGE_ACTIVATED)
                    or (stage == STAGE_LOCKED_GROUP and row['has_order'])):
                waiting = '—'
            else:
                waiting = _waiting_on(reads, pk, stage)

        label = row['project_id']
        if row['site_name']:
            label = f"{label} · {row['site_name']}"
        item = {
            'pk': pk, 'project_id': row['project_id'], 'site': label,
            'url': reverse('project_overview', args=[row['project_id']]),
            'program_pk': row['program_id'], 'tender': row['program_name'] or '—',
            'stage': stage, 'stage_code': f'S{stage_number[stage]}',
            'stage_label': STAGE_LABELS[stage],
            'design_status': DESIGN_STATUS_LABELS.get(info.design_status, '—'),
            'days': days, 'in_stage': in_stage, 'dc_capacity_kw': row['dc_capacity_kw'],
            'waiting_on': waiting, 'stuck': entry is not None,
            'rule': entry['rule'] if entry else None,
            'clock_date': entry['clock_date'] if entry else None,
            'overshoot': entry['overshoot'] if entry else None,
        }
        (stuck_rows if entry else other_rows).append(item)

    stuck_rows.sort(key=lambda r: (-r['overshoot'], r['tender'], r['site']))
    other_rows.sort(key=lambda r: (r['days'] is None, -(r['days'] or 0), r['project_id']))
    return stuck_rows + other_rows
