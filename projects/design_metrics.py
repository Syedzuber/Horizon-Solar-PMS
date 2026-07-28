"""
Read-only metrics for the Design Head's per-tender dashboard (Part 5).

NOTHING IN THIS MODULE WRITES. No save(), no create(), no update(), no delete() — every
function takes rows that already exist and returns numbers. If a future change needs a
write to make a metric work, the metric is wrong, not the rule.

WHY A SEPARATE MODULE FROM design_views.py
------------------------------------------
design_views.py owns the workflow: it decides who may act and what a status becomes.
This module owns arithmetic over the result. Keeping them apart means a reporting change
can never accidentally alter a transition, and the overdue rule below has exactly one
home rather than being re-derived on each surface that needs it.

QUERY SHAPE
-----------
`tender_metrics()` issues a fixed handful of queries regardless of how many sites the
tender has: the assignments, their attempts, the current approved Arkas, and the due-date
commitments — each one batched with `__in` — and then computes everything in Python. It
does NOT query per site. A 200-site tender costs the same as a 5-site one.

UNITS — READ THIS BEFORE TOUCHING THE CAPACITY PANEL
----------------------------------------------------
    Program.total_capacity      is MEGAWATTS   (models.py:204, "Total planned capacity in MW")
    ArkaSubmission.capacity_kw  is KILOWATTS
    Project.capacity_kw         is KILOWATTS

They differ by 1000 and nothing in the schema stops you comparing them directly. Every
conversion in this module goes through `_mw_to_kw()` so there is one place to be wrong,
and every figure that leaves here is named `*_kw` so a caller cannot lose track.
"""
from decimal import Decimal

from django.utils import timezone

from .models import (
    ArkaSubmission, DesignAssignment, DesignAttempt, DueDateCommitment,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_QC_FAILED,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    ARKA_APPROVED, ARKA_PENDING,
    ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
)

KW_PER_MW = Decimal('1000')


def _mw_to_kw(value_mw):
    """The single conversion point between Program.total_capacity (MW) and every design
    capacity figure (kW). Returns None for a tender with no recorded capacity, which is
    a real and common state — see `capacity_panel()`."""
    if value_mw is None:
        return None
    return Decimal(value_mw) * KW_PER_MW


# ---------------------------------------------------------------------------
# 1. The overdue rule — ONE definition, used by every surface
# ---------------------------------------------------------------------------

def is_overdue(assignment, current_commitment, today=None):
    """Settled decision 3 and 4, in one place.

        overdue  =  an APPROVED due-date commitment exists
                    AND its date is strictly before today
                    AND the assignment is not released

    THREE THINGS THIS DELIBERATELY DOES NOT DO:

      * It does not extend the due date for time spent blocked. A blocked site that has
        run past its date IS overdue and is counted as both — the Head sees the overdue
        number and the blocked number side by side and judges. Silently sliding the date
        would make the overdue count unauditable: nobody could reconstruct why a site
        was or was not counted on a given day.
      * It does not treat a site with no approved due date as overdue. There is no
        promise to have missed yet; that site is counted under its stage instead, and
        `stage_counts()` reports how many are in that position.
      * It does not treat a PROPOSED-but-unapproved date as a commitment. The handshake
        is two-sided (Part 2); one side proposing is not an agreement.

    `current_commitment` is passed in rather than looked up so callers that already hold
    the row do not re-query per site — this function is called once per assignment in a
    loop over hundreds of them.
    """
    if assignment.status == DESIGN_RELEASED:
        return False
    if current_commitment is None or current_commitment.approved_at is None:
        return False
    return current_commitment.proposed_date < (today or timezone.localdate())


def days_overdue(current_commitment, today=None):
    """How many days past the approved date. Callers must have already established that
    the assignment IS overdue via is_overdue(); this does the subtraction only."""
    if current_commitment is None:
        return 0
    return ((today or timezone.localdate()) - current_commitment.proposed_date).days


# ---------------------------------------------------------------------------
# 2. Stage definitions
# ---------------------------------------------------------------------------
# Settled decision 2: five different bottlenecks stay five different numbers. A single
# "pending" total would hide which one is jammed, which is the only thing the Head
# actually needs to know from this row.
#
# Two of these are NOT plain status lookups and are resolved in _classify() instead:
#   'in_design'      splits by whether the site has ever had an Arka submitted
#   'arka_approved'  is status=arka_submitted whose current Arka is already approved,
#                    i.e. the designer is mid-artifacts rather than waiting on the Head
STAGE_ORDER = [
    ('awaiting_survey',     'Awaiting survey'),
    ('awaiting_allocation', 'Awaiting allocation'),
    ('allocated',           'Allocated, no date proposed'),
    ('due_date_proposed',   'Date proposed, awaiting approval'),
    ('in_design',           'In design, awaiting Arka'),
    ('arka_submitted',      'Arka awaiting verdict'),
    ('arka_approved',       'Arka approved, artifacts incomplete'),
    ('artifacts_uploaded',  'Awaiting QC'),
    ('in_qc',               'In QC'),
    ('blocked',             'Blocked'),
    ('released',            'Released'),
]
STAGE_LABELS = dict(STAGE_ORDER)

# Stages where the ball is in the DESIGN HEAD's court. Used by the attention list and the
# review-queue panel — these are the only ones he can clear himself.
HEAD_ACTION_STAGES = ('awaiting_allocation', 'due_date_proposed', 'arka_submitted',
                      'artifacts_uploaded')


def _classify(assignment, current_arka):
    """Which stage bucket one assignment falls in. Exactly one, always."""
    status = assignment.status
    if status == DESIGN_SURVEY_RETURNED:
        return 'blocked'
    if status == DESIGN_ARKA_SUBMITTED:
        # Approved Arka means the Head has done his part and the designer owes artifacts;
        # pending means the Head owes a verdict. Same status, opposite bottleneck.
        if current_arka is not None and current_arka.verdict == ARKA_APPROVED:
            return 'arka_approved'
        return 'arka_submitted'
    if status in (DESIGN_ARKA_REJECTED, DESIGN_QC_FAILED):
        # Both hand the site back to the designer to produce a new Arka, which is what
        # 'in design, awaiting Arka' means. Keeping them as separate tiles would split
        # one bottleneck across three columns for no operational gain.
        return 'in_design'
    if status in STAGE_LABELS:
        return status
    return 'in_design'


# ---------------------------------------------------------------------------
# 3. The one entry point
# ---------------------------------------------------------------------------

def tender_metrics(program, today=None):
    """Every number the Design Head's tender dashboard shows, in a fixed query budget.

    Returns a dict; see the panel builders below for each key's shape. Safe on a tender
    with no design work at all — every panel degrades to zeros and empty lists rather
    than raising, because a freshly created tender is a normal state, not an error.
    """
    today = today or timezone.localdate()

    # ---- the four batched reads (plus one count) -------------------------------
    assignments = list(
        DesignAssignment.objects
        .filter(project__program=program, project__is_deleted=False)
        .select_related('project', 'assigned_to__user')
        .order_by('project__project_id')
    )
    assignment_ids = [a.pk for a in assignments]

    attempts = list(DesignAttempt.objects.filter(assignment_id__in=assignment_ids))
    arkas = list(
        ArkaSubmission.objects
        .filter(attempt__assignment_id__in=assignment_ids, is_current=True)
        .select_related('attempt')
    )
    commitments = list(
        DueDateCommitment.objects.filter(assignment_id__in=assignment_ids)
    )
    total_sites = program.sites.filter(is_deleted=False).count()

    # ---- index in Python; no further queries ------------------------------------
    attempts_by_assignment = {}
    for t in attempts:
        attempts_by_assignment.setdefault(t.assignment_id, []).append(t)

    # The CURRENT attempt is the one whose number matches the assignment's pointer —
    # read from the pointer rather than max(), so this agrees with design_views.
    current_attempt_id = {}
    for a in assignments:
        for t in attempts_by_assignment.get(a.pk, []):
            if t.attempt_number == a.current_attempt_number:
                current_attempt_id[a.pk] = t.pk
                break

    arka_by_attempt = {k.attempt_id: k for k in arkas}
    current_arka = {a.pk: arka_by_attempt.get(current_attempt_id.get(a.pk))
                    for a in assignments}

    commitments_by_assignment = {}
    for c in commitments:
        commitments_by_assignment.setdefault(c.assignment_id, []).append(c)
    current_commitment = {}
    for a in assignments:
        rows = commitments_by_assignment.get(a.pk, [])
        current_commitment[a.pk] = next((c for c in rows if c.is_current), None)

    # ---- per-site facts every panel reuses --------------------------------------
    sites = []
    for a in assignments:
        arka = current_arka.get(a.pk)
        commitment = current_commitment.get(a.pk)
        approved_arka = arka if (arka is not None and arka.verdict == ARKA_APPROVED) else None
        overdue = is_overdue(a, commitment, today)
        sites.append({
            'assignment':    a,
            'project':       a.project,
            'designer':      a.assigned_to,
            'stage':         _classify(a, arka),
            'arka':          arka,
            'approved_arka': approved_arka,
            'capacity_kw':   approved_arka.capacity_kw if approved_arka else None,
            'commitment':    commitment,
            'has_approved_due': bool(commitment and commitment.approved_at),
            'overdue':       overdue,
            'days_over':     days_overdue(commitment, today) if overdue else 0,
            'blocked':       a.status == DESIGN_SURVEY_RETURNED,
            'revisions':     max(len(commitments_by_assignment.get(a.pk, [])) - 1, 0),
            'attempts':      attempts_by_assignment.get(a.pk, []),
            'released':      a.status == DESIGN_RELEASED,
        })

    return {
        'program':       program,
        'today':         today,
        'sites':         sites,
        'total_sites':   total_sites,
        'assigned_sites': len(assignments),
        # A tender's sites can exist without a DesignAssignment row (one is created by the
        # first survey upload). Reported rather than hidden, so the stage counts summing to
        # fewer than the site count is explained on the page instead of looking like a bug.
        'unassigned_sites': max(total_sites - len(assignments), 0),
        'stages':        stage_counts(sites),
        'workload':      designer_workload(sites, today),
        'capacity':      capacity_panel(program, sites, total_sites),
        'queue':         review_queue_age(sites, today),
        'attention':     attention_list(sites, today),
        'no_due_date':   sum(1 for s in sites if not s['has_approved_due'] and not s['released']),
    }


# ---------------------------------------------------------------------------
# 4. Panels
# ---------------------------------------------------------------------------

def stage_counts(sites):
    """One count per stage, in workflow order, never merged (settled decision 2).

    Every assignment lands in exactly one bucket, so these sum to len(sites) — the
    dashboard asserts that on screen rather than trusting it.
    """
    counts = {key: 0 for key, _ in STAGE_ORDER}
    for s in sites:
        counts[s['stage']] += 1
    return [
        {'key': key, 'label': label, 'count': counts[key],
         'is_head': key in HEAD_ACTION_STAGES}
        for key, label in STAGE_ORDER
    ]


def designer_workload(sites, today=None):
    """One row per designer holding at least one assignment on this tender.

    SITES AND kW SIT SIDE BY SIDE ON PURPOSE. Sites are not equal units of work;
    allocating by count alone hands one designer six ground mounts and another six
    rooftops and calls them balanced.

    REWORK IS SPLIT AND NEVER MERGED (settled decision 5). `qc_failed` is the design
    being wrong; `pm_change_request` is the brief moving. A team with a high first number
    needs coaching; a team with a high second number needs the customer pinned down, and
    coaching them would be both useless and unfair. The multiplier is
    total attempts ÷ released sites — undefined with no released sites, and reported as
    None (rendered '—') rather than 0, which would read as "no rework" when it means
    "no data".
    """
    by_designer = {}
    for s in sites:
        designer = s['designer']
        if designer is None:
            continue
        row = by_designer.setdefault(designer.pk, {
            'designer': designer, 'sites': 0, 'capacity_kw': Decimal('0'),
            'sites_no_capacity': 0,
            'overdue': 0, 'blocked': 0, 'released': 0,
            'attempts': 0, 'qc_failed': 0, 'pm_change_request': 0,
        })
        if s['released']:
            row['released'] += 1
        else:
            # "Sites" and "kW" describe CURRENT LOAD, so released work is excluded from
            # both — a designer is not still carrying a site they have finished.
            row['sites'] += 1
            if s['capacity_kw'] is not None:
                row['capacity_kw'] += s['capacity_kw']
            else:
                # Counted so the template can say WHY the kW figure is low. A designer
                # holding two sites that have no approved Arka yet legitimately shows
                # 0.00 kW, which reads as "no load" unless the reason is on screen.
                row['sites_no_capacity'] += 1
        if s['overdue']:
            row['overdue'] += 1
        if s['blocked']:
            row['blocked'] += 1
        # Rework counts every attempt the designer has worked, released or not.
        row['attempts'] += len(s['attempts'])
        for t in s['attempts']:
            if t.opened_reason == ATTEMPT_REASON_QC_FAILED:
                row['qc_failed'] += 1
            elif t.opened_reason == ATTEMPT_REASON_PM_CHANGE_REQUEST:
                row['pm_change_request'] += 1

    rows = []
    for row in by_designer.values():
        row['rework'] = (round(row['attempts'] / row['released'], 1)
                         if row['released'] else None)
        rows.append(row)
    # Default order is kW descending — the load that matters, not the row count.
    rows.sort(key=lambda r: (r['capacity_kw'], r['sites']), reverse=True)
    return rows


def capacity_panel(program, sites, total_sites):
    """Designed versus tendered, with the projection labelled as an extrapolation.

    ONLY THE CURRENT APPROVED ARKA COUNTS (settled decision 6). A site whose Arka is
    pending or rejected contributes nothing and is excluded from the average, because a
    number the Head has not approved is not a designed capacity yet.

    `tendered_kw` is None when the tender has no recorded capacity — which is the case
    for real tenders in this database today. The panel must then show designed capacity
    alone and say so; it must NOT invent a target or fall back to planned_site_count
    arithmetic.
    """
    contributing = [s for s in sites if s['capacity_kw'] is not None]
    designed_kw = sum((s['capacity_kw'] for s in contributing), Decimal('0'))
    n = len(contributing)
    average_kw = (designed_kw / n) if n else None

    tendered_kw = _mw_to_kw(program.total_capacity)

    # Extrapolation: average designed capacity × every site in the tender. Honest only
    # if it is labelled as a projection, which the template does.
    projected_kw = (average_kw * total_sites) if (average_kw is not None and total_sites) else None
    shortfall_kw = ((tendered_kw - projected_kw)
                    if (tendered_kw is not None and projected_kw is not None) else None)

    return {
        'tendered_recorded': tendered_kw is not None,
        'tendered_kw':   tendered_kw,
        'designed_kw':   designed_kw,
        'average_kw':    average_kw,
        'projected_kw':  projected_kw,
        'shortfall_kw':  shortfall_kw,
        'shortfall_is_deficit': bool(shortfall_kw is not None and shortfall_kw > 0),
        'contributing':  n,
        'total_sites':   total_sites,
        # Shown next to the projection so its weight is visible: 2 of 200 sites is a very
        # different claim from 150 of 200, and the number alone does not say which.
        'coverage_pct':  round(100 * n / total_sites, 1) if total_sites else 0,
    }


def review_queue_age(sites, today=None):
    """How long the Head's OWN queue has been waiting.

    He is the single approver at both gates, so a burst at the end of a tender phase
    lands entirely on him. Ages are measured from the moment the item entered his queue —
    Arka submitted_at, and qc_started_at is deliberately NOT used for the QC figure
    because a package awaiting QC has not started it yet; `updated_at` on the assignment
    is when it reached artifacts_uploaded.
    """
    today = today or timezone.localdate()

    pending_arka = [s for s in sites if s['stage'] == 'arka_submitted' and s['arka']]
    awaiting_qc  = [s for s in sites if s['stage'] == 'artifacts_uploaded']

    def _age_days(dt):
        return (today - timezone.localtime(dt).date()).days if dt else 0

    oldest_arka = min(pending_arka, key=lambda s: s['arka'].submitted_at, default=None)
    oldest_qc   = min(awaiting_qc, key=lambda s: s['assignment'].updated_at, default=None)

    return {
        'arka_count':      len(pending_arka),
        'arka_oldest_days': _age_days(oldest_arka['arka'].submitted_at) if oldest_arka else None,
        'arka_oldest_site': oldest_arka['project'] if oldest_arka else None,
        'qc_count':        len(awaiting_qc),
        'qc_oldest_days':  _age_days(oldest_qc['assignment'].updated_at) if oldest_qc else None,
        'qc_oldest_site':  oldest_qc['project'] if oldest_qc else None,
    }


# Severity bands for the attention list. Lower sorts first.
_SEV_OVERDUE  = 0
_SEV_BLOCKED  = 1
_SEV_REVISED  = 2
_SEV_HEAD     = 3

ATTENTION_LIMIT = 10


def attention_list(sites, today=None, limit=ATTENTION_LIMIT):
    """At most ten rows, worst first. A work queue, not a report (settled decision 8).

    Ordering is by severity band, then by the magnitude that makes a row urgent within
    its band — days overdue, then days blocked, then revision count, then queue age. A
    site can qualify on several grounds; it appears ONCE, under its worst.
    """
    today = today or timezone.localdate()
    rows = []
    seen = set()

    def _add(site, severity, magnitude, reason, marker):
        if site['assignment'].pk in seen:
            return
        seen.add(site['assignment'].pk)
        rows.append({
            'project': site['project'], 'designer': site['designer'],
            'severity': severity, 'magnitude': magnitude,
            'reason': reason, 'marker': marker,
            'stage': STAGE_LABELS.get(site['stage'], site['stage']),
        })

    for s in sorted((x for x in sites if x['overdue']),
                    key=lambda x: x['days_over'], reverse=True):
        attempt_no = s['assignment'].current_attempt_number or 1
        _add(s, _SEV_OVERDUE, s['days_over'],
             f'{s["days_over"]} day(s) past the agreed date, on attempt {attempt_no}',
             'danger')

    for s in sites:
        if not s['blocked']:
            continue
        started = s['assignment'].survey_returned_at
        days = (today - timezone.localtime(started).date()).days if started else 0
        reason = (s['assignment'].survey_return_reason or '').strip() or 'no reason recorded'
        _add(s, _SEV_BLOCKED, days, f'Blocked {days} day(s) — {reason}', 'warning')

    for s in sorted((x for x in sites if x['revisions'] >= 3 and not x['released']),
                    key=lambda x: x['revisions'], reverse=True):
        _add(s, _SEV_REVISED, s['revisions'],
             f'Due date revised {s["revisions"]} times', 'warning')

    # Longest-waiting items in the Head's own queue, so his backlog is on the same list
    # as the designers' — he is a bottleneck like any other.
    #
    # An Arka's wait is dated from ITS OWN submitted_at, not from assignment.updated_at,
    # so this agrees with review_queue_age() above. They diverge in practice: updated_at
    # moves on any assignment save, while submitted_at is when the item actually entered
    # the queue, and two panels reporting different ages for the same item is worse than
    # either number being slightly off.
    head_waiting = [s for s in sites if s['stage'] in HEAD_ACTION_STAGES]

    def _waiting_since(site):
        if site['stage'] == 'arka_submitted' and site['arka'] is not None:
            return site['arka'].submitted_at
        return site['assignment'].updated_at

    for s in sorted(head_waiting, key=_waiting_since):
        days = (today - timezone.localtime(_waiting_since(s)).date()).days
        _add(s, _SEV_HEAD, days,
             f'Waiting on you — {STAGE_LABELS.get(s["stage"], s["stage"]).lower()}, '
             f'{days} day(s)', 'secondary')

    rows.sort(key=lambda r: (r['severity'], -r['magnitude']))
    return rows[:limit]
