"""
TB-1 — how far a set of activated sites has really moved. One helper, no view.

    b = site_breakdown(program.sites.all(), timezone.now())
    b.in_execution, b.mobilized, b.active, b.active_today     # counts
    b.in_execution_ids, b.mobilized_ids, ...                  # the sites behind each

Used by the tender page (views.program_detail); the daily snapshot (SNAP-1b) will call it
too, so the two can never count differently.

THE FOUR FIGURES (Zuber's rulings B1-B4, 14 Nov 2026)
    in_execution  the Site Dashboard's "Sites in execution": permissions.dashboard_live_sites()
                  (D9: not deleted, not test, not Cancelled), less Drafts. Not re-derived.
    mobilized     in execution AND a delivery challan that is not Rejected AND design started.
                  "Design started" is the CEO pipeline's own mapping, tender_stages
                  .STATUS_TO_STAGE, stages S2-S5 (In design .. Released), so the system has
                  one definition of it. A site with no DesignAssignment has not started.
    active        in execution AND the site's Civil Work and MMS Installation task — the
                  scheduler's ANCHOR, by template code, never by name — is In Progress or
                  Done and not N/A. A due date alone does not count; Blocked does not count
                  (the dashboard's lane rule starts a lane on In Progress / Done only).
    active_today  in execution AND a human activity row (site_dashboard.human_activity_q,
                  the dashboard's "Latest activity" rule) on the IST calendar day of `now`,
                  from 00:00 IST up to `now` (TB-5 ruling A1, 18 Nov 2026; it was the 24
                  hours up to `now` until then). The day is TB-3's attendance day, so Active
                  today and Attendance today cut at the same midnight (A2).
                  Contractor attendance writes no ActivityLog, so it never counts (5 Nov
                  rule). Money rows count: only the number is shown, never the row.
                  NOT a subset of `active`: shown apart from the chain (B5).

TB-3 — DESIGN, PROCUREMENT AND ATTENDANCE (Zuber's rulings G1-G6 and Q1-Q3, 17 Nov 2026)
Over the same sites in execution (G1). The tender page shows them; TB-4 (Site Dashboard)
and SNAP-1c (snapshot) will read the same fields.
    design_not_started / _in_progress / _released
                  the site's DesignAssignment status, bucketed by its CEO pipeline stage
                  (tender_stages.STATUS_TO_STAGE): S0-S1 not started (Design Hold is S0),
                  S2-S4 in progress, S5 released. Looked up at call time, so a status moved
                  in the mapping moves here too. A site with no DesignAssignment is in none.
    received / pending
                  among MOBILIZED sites only (G3): received = any line in the four component
                  categories with a received quantity above 0, on a challan of ANY status,
                  Rejected included — models.first_component_received(), the phase rule R3
                  (ruling Q1: Rejected is repurposed to "shortfall AND damage", so material
                  can be on site under a Rejected challan). pending = mobilized - received.
    dcs_to_confirm / dcs_overdue
                  COUNTS OF CHALLANS (G4), not sites: Expected or Partially Received, i.e.
                  the SE still has receipts to record; overdue when expected_delivery_date
                  is before today (IST). No expected date is never overdue. The id lists are
                  the sites holding them (ruling Q3: one query cannot list challan ids).
    dcs_to_confirm_by_site / dcs_overdue_by_site
                  TB-4 (ruling Q1(a), 17 Nov): the same counts per site, as (site pk, n)
                  pairs, so the Site Dashboard can cut them to RESCO sites. The per-site
                  numbers were already columns of the query; they are now kept rather than
                  only summed. Their n's add up to dcs_to_confirm / dcs_overdue.
    attendance_today
                  a non-voided ContractorAttendance dated today, the IST calendar day of
                  `now` (G5) — the "logged" half of attendance_alerts.sites_missing_attendance.

ONE QUERY whatever the number of sites: every condition is an Exists / Subquery / join on
the same statement over the sites in execution. tests_site_breakdown pins it at 1 for 1 and
for 50 sites; tests_site_breakdown_extra pins it again with the TB-3 columns.

TWO RULES ARE COPIED, NOT CALLED (ruling Q2). first_component_received() and the
attendance check inside sites_missing_attendance() each answer for one site, or inside a
query of their own; calling them would cost a query per site. Each copy below names its
original, and tests_site_breakdown_extra asserts the copy agrees with the original on
every test site, so a change to either side fails a test.
"""
from dataclasses import dataclass
from datetime import datetime, time

from django.db.models import Count, Exists, F, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (DC_CATEGORY_TO_MIRROR_CODE, ActivityLog, ContractorAttendance,
                     DCLineItem, DeliveryChallan, Task)
from .permissions import dashboard_live_sites
from .scheduling import ANCHOR_TASK_CODE
from .site_dashboard import human_activity_between, in_block_q
from .tender_stages import (STAGE_AWAITING_PM, STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_NO_SURVEY,
                            STAGE_RELEASED, STAGE_SURVEY_ON_FILE, STATUS_TO_STAGE)

#: A Draft site is not in execution: the dashboard's SiteCard.is_draft test
#: (site_dashboard._card, `status == 'Draft'`), whose cards the view leaves out of the
#: "Sites in execution" tile. tests_site_breakdown pins the count equal to that tile.
DRAFT_STATUS = 'Draft'

#: The CEO pipeline stages that mean design work has started (ruling P3): S2 In design,
#: S3 In QC / Head QC, S4 Awaiting PM approval, S5 Released. S0 (no survey, and Design
#: Hold) and S1 (survey on file, not allocated) have not started.
DESIGN_STARTED_STAGES = frozenset({STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_AWAITING_PM,
                                   STAGE_RELEASED})

#: DesignAssignment.status values that count as "design started", derived from the
#: mapping rather than listed, so a status added to STATUS_TO_STAGE is placed once.
DESIGN_STARTED_STATUSES = frozenset(
    status for status, stage in STATUS_TO_STAGE.items() if stage in DESIGN_STARTED_STAGES)

#: The Civil task states that make a site Active.
CIVIL_STARTED_STATUSES = (Task.IN_PROGRESS, Task.DONE)

#: TB-3 design buckets, as CEO pipeline STAGES (ruling G2). The statuses in each are read
#: from STATUS_TO_STAGE when site_breakdown runs (design_bucket), never listed here.
#: In progress + released is exactly DESIGN_STARTED_STAGES above.
DESIGN_NOT_STARTED_STAGES = frozenset({STAGE_NO_SURVEY, STAGE_SURVEY_ON_FILE})
DESIGN_IN_PROGRESS_STAGES = frozenset({STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_AWAITING_PM})
DESIGN_RELEASED_STAGES = frozenset({STAGE_RELEASED})

BUCKET_NOT_STARTED, BUCKET_IN_PROGRESS, BUCKET_RELEASED = 'not_started', 'in_progress', 'released'

#: The challan states that leave the site engineer receipts to record (ruling G4).
#: Received and Rejected are closed.
DC_TO_CONFIRM_STATUSES = (DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED)


def design_bucket(status):
    """The TB-3 design bucket of a DesignAssignment status, or None for no assignment.

    Indexes STATUS_TO_STAGE rather than .get(), as tender_stages does: an unmapped status
    must fail loudly, not fall into a bucket by default."""
    if status is None:
        return None
    stage = STATUS_TO_STAGE[status]
    if stage in DESIGN_NOT_STARTED_STAGES:
        return BUCKET_NOT_STARTED
    if stage in DESIGN_IN_PROGRESS_STAGES:
        return BUCKET_IN_PROGRESS
    if stage in DESIGN_RELEASED_STAGES:
        return BUCKET_RELEASED
    # S6 / S7 are never values of STATUS_TO_STAGE (groups and activation override it in
    # tender_stages._stage_of), so this is a mapping change nobody has ruled on.
    raise ValueError(f'design status {status!r} maps to stage {stage!r}, which has no TB-3 bucket')


def _challan_count(challans):
    """A per-site count of `challans` (a queryset correlated on OuterRef('pk')) as one
    column of the outer statement, 0 when the site has none. Grouped by project inside the
    subquery so it never fans out the site rows."""
    return Coalesce(Subquery(challans.order_by().values('project_id')
                             .annotate(n=Count('pk')).values('n')[:1],
                             output_field=IntegerField()),
                    Value(0))


@dataclass(frozen=True)
class SiteBreakdown:
    """The figures as tuples of site pks (ascending); the counts are their lengths, except
    dcs_to_confirm / dcs_overdue, which count challans (TB-3 ruling G4).

    The TB-3 fields have defaults so a caller (or a test stub) building the TB-1 four
    positionally still works; site_breakdown() always fills every field."""
    in_execution_ids: tuple
    mobilized_ids: tuple
    active_ids: tuple
    active_today_ids: tuple
    design_not_started_ids: tuple = ()
    design_in_progress_ids: tuple = ()
    design_released_ids: tuple = ()
    received_ids: tuple = ()
    pending_ids: tuple = ()
    dcs_to_confirm_site_ids: tuple = ()   # sites holding a challan to confirm
    dcs_overdue_site_ids: tuple = ()      # sites holding an overdue one
    attendance_today_ids: tuple = ()
    dcs_to_confirm: int = 0               # challans, not sites
    dcs_overdue: int = 0                  # challans, not sites
    dcs_to_confirm_by_site: tuple = ()    # TB-4: (site pk, challans) for each site holding any
    dcs_overdue_by_site: tuple = ()       # TB-4: (site pk, overdue challans), same shape

    @property
    def in_execution(self):
        return len(self.in_execution_ids)

    @property
    def mobilized(self):
        return len(self.mobilized_ids)

    @property
    def active(self):
        return len(self.active_ids)

    @property
    def active_today(self):
        return len(self.active_today_ids)

    @property
    def design_not_started(self):
        return len(self.design_not_started_ids)

    @property
    def design_in_progress(self):
        return len(self.design_in_progress_ids)

    @property
    def design_released(self):
        return len(self.design_released_ids)

    @property
    def received(self):
        return len(self.received_ids)

    @property
    def pending(self):
        return len(self.pending_ids)

    @property
    def attendance_today(self):
        return len(self.attendance_today_ids)


def site_breakdown(projects, now):
    """Break `projects` down into in execution / mobilized / active / active today as of
    `now` (an aware datetime). Pure read, one query, no side effects.

    `projects` is anything a `pk__in` lookup takes: a Project queryset (program.sites.all()),
    a list of Projects or a list of pks. Sites outside dashboard_live_sites() or in Draft
    are simply not counted, so a caller never has to pre-filter.
    """
    # The IST calendar day of `now` (settings.TIME_ZONE), not of the server clock: the
    # attendance day, the overdue cut-off and Active today all follow the caller's moment.
    today = timezone.localdate(now)
    # Active today starts at 00:00 IST on that same `today` (TB-5 ruling A2), built from it
    # rather than computed apart, so it can never cut at a different midnight from
    # Attendance today. A known gap, accepted by Zuber: the 8 PM snapshot counts 00:00-20:00,
    # so an update made after 20:00 IST appears in no snapshot.
    day_start = timezone.make_aware(datetime.combine(today, time.min))

    # Challans the site engineer still has receipts to record on (TB-3 ruling G4).
    to_confirm = DeliveryChallan.objects.filter(project_id=OuterRef('pk'),
                                                status__in=DC_TO_CONFIRM_STATUSES)

    # The site's Civil anchor, chosen exactly as site_dashboard._schedule_reasons() and
    # scheduling._plan() choose it: in an active block, built from a template row, not a
    # mirror, not a location copy (in_block_q), code CIVIL_WORK_AND_MMS_INSTALLATION, the
    # first by (phase_order, task_order, pk). Its status and N/A flag are read as two scalar
    # subqueries so a second Civil row can never stand in for an N/A first one.
    anchor = (Task.objects.filter(phase__project_id=OuterRef('pk'))
              .filter(in_block_q(), template_task__code=ANCHOR_TASK_CODE)
              .order_by('phase__phase_order', 'task_order', 'pk'))

    # Every site in execution among `projects`, with each condition as a column of the same
    # statement: the challan and the activity as EXISTS, the design status through the
    # reverse OneToOne (a LEFT JOIN, at most one row, no fan-out), the anchor as above.
    rows = (dashboard_live_sites()
            .filter(pk__in=projects)
            .exclude(status=DRAFT_STATUS)
            .annotate(
                # Any challan but a Rejected one. DeliveryChallan has no void or delete
                # field, so Rejected is the only "not a real delivery" state (pre-flight P3).
                has_dc=Exists(DeliveryChallan.objects.filter(project_id=OuterRef('pk'))
                              .exclude(status=DeliveryChallan.REJECTED)),
                design_status=F('design_assignment__status'),
                civil_status=Subquery(anchor.values('status')[:1]),
                civil_na=Subquery(anchor.values('is_not_applicable')[:1]),
                # The dashboard's latest-activity rows (human_activity_q), with no money
                # filter: the money rule hides a row's content, and only a count is shown.
                # Today's rows only: from 00:00 IST (inclusive) up to `now`. A range on the
                # raw timestamp rather than timestamp__date, so no per-row time-zone cast.
                # Bounded above by `now` so a fixed `now` (the snapshot, tests) is honest.
                # Through the shared bounded builder (RPT-B1, D7), inclusive of `now`.
                touched=Exists(ActivityLog.objects
                               .filter(project_id=OuterRef('pk'))
                               .filter(human_activity_between(day_start, now,
                                                              end_inclusive=True))),
                # COPY OF models.first_component_received() (phase rule R3), as a column so
                # one statement answers for every site (ruling Q2): a line in a
                # DC_CATEGORY_TO_MIRROR_CODE category with received_quantity > 0, on a
                # challan of ANY status — Rejected included (ruling Q1, 20 Oct rule). Keep
                # the two in step; tests_site_breakdown_extra fails if they disagree.
                material_received=Exists(DCLineItem.objects.filter(
                    challan__project_id=OuterRef('pk'),
                    boq_category__in=list(DC_CATEGORY_TO_MIRROR_CODE),
                    received_quantity__gt=0)),
                dcs_to_confirm=_challan_count(to_confirm),
                dcs_overdue=_challan_count(to_confirm.filter(expected_delivery_date__lt=today)),
                # COPY OF the "logged" check inside attendance_alerts.sites_missing_attendance()
                # (its `logged_today`): a non-voided ContractorAttendance dated `today`. That
                # check lives inside the alert's own query, so there is nothing to call
                # (ruling Q2). tests_site_breakdown_extra fails if the two disagree.
                attended=Exists(ContractorAttendance.objects.filter(
                    project_id=OuterRef('pk'), date=today, is_voided=False)),
            )
            .order_by('pk')
            .values('pk', 'has_dc', 'design_status', 'civil_status', 'civil_na', 'touched',
                    'material_received', 'dcs_to_confirm', 'dcs_overdue', 'attended'))

    in_execution, mobilized, active, active_today = [], [], [], []
    design = {BUCKET_NOT_STARTED: [], BUCKET_IN_PROGRESS: [], BUCKET_RELEASED: []}
    received, pending, attendance_today = [], [], []
    # (pk, n) per site, kept for TB-4's RESCO cut; the site lists are their first elements.
    to_confirm_sites, overdue_sites = [], []
    dcs_to_confirm = dcs_overdue = 0
    for row in rows:
        pk = row['pk']
        in_execution.append(pk)
        if row['has_dc'] and row['design_status'] in DESIGN_STARTED_STATUSES:
            mobilized.append(pk)
            # Received / pending split Mobilized and nothing else (ruling G3), so the two
            # always add up to it; material on a site that is not mobilized is in neither.
            (received if row['material_received'] else pending).append(pk)
        if row['civil_status'] in CIVIL_STARTED_STATUSES and not row['civil_na']:
            active.append(pk)
        if row['touched']:
            active_today.append(pk)
        bucket = design_bucket(row['design_status'])
        if bucket is not None:
            design[bucket].append(pk)
        if row['dcs_to_confirm']:
            to_confirm_sites.append((pk, row['dcs_to_confirm']))
            dcs_to_confirm += row['dcs_to_confirm']
        if row['dcs_overdue']:
            overdue_sites.append((pk, row['dcs_overdue']))
            dcs_overdue += row['dcs_overdue']
        if row['attended']:
            attendance_today.append(pk)
    return SiteBreakdown(
        tuple(in_execution), tuple(mobilized), tuple(active), tuple(active_today),
        design_not_started_ids=tuple(design[BUCKET_NOT_STARTED]),
        design_in_progress_ids=tuple(design[BUCKET_IN_PROGRESS]),
        design_released_ids=tuple(design[BUCKET_RELEASED]),
        received_ids=tuple(received),
        pending_ids=tuple(pending),
        dcs_to_confirm_site_ids=tuple(pk for pk, _ in to_confirm_sites),
        dcs_overdue_site_ids=tuple(pk for pk, _ in overdue_sites),
        attendance_today_ids=tuple(attendance_today),
        dcs_to_confirm=dcs_to_confirm,
        dcs_overdue=dcs_overdue,
        dcs_to_confirm_by_site=tuple(to_confirm_sites),
        dcs_overdue_by_site=tuple(overdue_sites),
    )
