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
                  the dashboard's "Latest activity" rule) in the 24 hours up to `now`.
                  Contractor attendance writes no ActivityLog, so it never counts (5 Nov
                  rule). Money rows count: only the number is shown, never the row.
                  NOT a subset of `active`: shown apart from the chain (B5).

ONE QUERY whatever the number of sites: every condition is an Exists / Subquery / join on
the same statement over the sites in execution. tests_site_breakdown pins it at 1 for 1 and
for 50 sites.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Exists, F, OuterRef, Subquery

from .models import ActivityLog, DeliveryChallan, Task
from .permissions import dashboard_live_sites
from .scheduling import ANCHOR_TASK_CODE
from .site_dashboard import human_activity_q, in_block_q
from .tender_stages import (STAGE_AWAITING_PM, STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_RELEASED,
                            STATUS_TO_STAGE)

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

#: "Active today" looks back this far from `now`: rolling, not the calendar day, so the
#: 8 PM snapshot still counts an update made at 9 PM the evening before (ruling B4).
ACTIVE_TODAY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class SiteBreakdown:
    """The four figures as tuples of site pks (ascending); the counts are their lengths."""
    in_execution_ids: tuple
    mobilized_ids: tuple
    active_ids: tuple
    active_today_ids: tuple

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


def site_breakdown(projects, now):
    """Break `projects` down into in execution / mobilized / active / active today as of
    `now` (an aware datetime). Pure read, one query, no side effects.

    `projects` is anything a `pk__in` lookup takes: a Project queryset (program.sites.all()),
    a list of Projects or a list of pks. Sites outside dashboard_live_sites() or in Draft
    are simply not counted, so a caller never has to pre-filter.
    """
    since = now - ACTIVE_TODAY_WINDOW

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
                # Bounded above by `now` so a fixed `now` (the snapshot, tests) is honest.
                touched=Exists(ActivityLog.objects
                               .filter(project_id=OuterRef('pk'), timestamp__gt=since,
                                       timestamp__lte=now)
                               .filter(human_activity_q())),
            )
            .order_by('pk')
            .values('pk', 'has_dc', 'design_status', 'civil_status', 'civil_na', 'touched'))

    in_execution, mobilized, active, active_today = [], [], [], []
    for row in rows:
        in_execution.append(row['pk'])
        if row['has_dc'] and row['design_status'] in DESIGN_STARTED_STATUSES:
            mobilized.append(row['pk'])
        if row['civil_status'] in CIVIL_STARTED_STATUSES and not row['civil_na']:
            active.append(row['pk'])
        if row['touched']:
            active_today.append(row['pk'])
    return SiteBreakdown(tuple(in_execution), tuple(mobilized), tuple(active),
                         tuple(active_today))
