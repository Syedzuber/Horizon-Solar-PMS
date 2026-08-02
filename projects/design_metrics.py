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
    ArkaSubmission, DesignAssignment, DesignAttempt, DesignChangeRequest,
    DueDateCommitment, CHANGE_REQUEST_PENDING,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_QC_FAILED,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    ARKA_APPROVED, ARKA_PENDING,
    ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    ERROR_GROUP_A, error_category_group,
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
# 0. Which commitment counts — the effective / pending split (Part 8)
# ---------------------------------------------------------------------------
# Part 2 had one notion of "the" commitment: the `is_current` row. That was enough
# because a site had either no agreed date or exactly one, and a proposal in flight
# meant there was no agreed date yet.
#
# Part 8 breaks that. The date is now auto-approved AT ALLOCATION, so a site always
# has an approved date, and the designer can afterwards request an EXTENSION — a new
# row that takes over `is_current` while sitting unapproved. If the surfaces kept
# reading `is_current`, requesting an extension would make the approved date vanish
# from every screen and, worse, drop the site out of the overdue count: `is_overdue`
# returns False for an unapproved commitment, so a designer could clear their own
# overdue flag simply by asking for more time. That is the failure this split exists
# to prevent.
#
# So the two questions are answered by two different functions:
#
#   effective_commitment()  what the site is ACTUALLY committed to. The most recently
#                           approved row, whether or not it is current. Every read
#                           surface uses this — due-date display, overdue, dashboard.
#   pending_extension()     the unapproved request awaiting a verdict, if any. Used
#                           only to render "extension requested" and to drive the
#                           Head's approve/reject buttons.
#
# `is_current` keeps its original job: it marks the row the approve/reject views act
# on, and the partial unique constraint still guarantees there is at most one.

def effective_commitment(rows):
    """The approved due date in force, from an already-loaded list of commitments.

    Ordered by `approved_at` rather than `is_current` so a pending extension cannot
    displace it, and by pk as a tiebreak so two rows approved in the same transaction
    still order deterministically.
    """
    approved = [c for c in rows if c.approved_at is not None]
    if not approved:
        return None
    return max(approved, key=lambda c: (c.approved_at, c.pk))


def pending_extension(rows):
    """The extension request awaiting a verdict, or None.

    An unapproved row that is NOT current is a request that was already rejected and
    stood down; it is history, not a pending request, so `is_current` is required here.
    """
    current = next((c for c in rows if c.is_current), None)
    if current is not None and current.approved_at is None:
        return current
    return None


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

    PART 8. Callers must pass the EFFECTIVE commitment — `effective_commitment(rows)`,
    the most recently approved row — NOT the `is_current` one. A pending extension
    request is current but unapproved, and passing it here would return False and quietly
    remove an overdue site from the count the moment its designer asked for more time.
    The `approved_at is None` guard below is kept as a backstop for exactly that mistake.

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
    ('arka_submitted',      'Arka awaiting Design QC'),
    ('awaiting_head_arka',  'Arka awaiting Design Head'),
    ('arka_approved',       'Arka approved, artifacts incomplete'),
    ('artifacts_uploaded',  'Awaiting Design QC'),
    ('in_qc',               'In Design QC'),
    ('awaiting_head_qc',    'Package awaiting Design Head'),
    # KEY STAYS 'blocked', LABEL BECOMES 'Design Hold' (Part 8). The key is not cosmetic:
    # the dashboard's drill-down puts it in the query string as ?stage=blocked, so
    # renaming it would break every link and bookmark already pointing at that filter for
    # no gain — nobody sees the key.
    ('blocked',             'Design Hold'),
    ('released',            'Released'),
]
STAGE_LABELS = dict(STAGE_ORDER)

# PART 9 SPLIT THE OLD `HEAD_ACTION_STAGES` IN TWO, because the two reviewers now hold
# different parts of what used to be one queue. Merging them again would put a Design QC
# backlog on the Head's attention list as though it were his to clear, which is the exact
# misattribution the second gate exists to make visible.
#
# Stages where the ball is in the DESIGN HEAD's court.
HEAD_ACTION_STAGES = ('awaiting_allocation', 'due_date_proposed',
                      'awaiting_head_arka', 'awaiting_head_qc')

# Stages where the ball is in DESIGN QC's court. Allocation and due dates are absent —
# those are the Head's alone and Part 9 does not move them.
QC_ACTION_STAGES = ('arka_submitted', 'artifacts_uploaded', 'in_qc')


def _classify(assignment, current_arka):
    """Which stage bucket one assignment falls in. Exactly one, always."""
    status = assignment.status
    if status == DESIGN_SURVEY_RETURNED:
        return 'blocked'
    if status == DESIGN_ARKA_SUBMITTED:
        # PART 9: the test is head_verdict, not verdict. `arka_submitted` carrying a
        # head-approved Arka means BOTH gates are done and the designer owes artifacts;
        # anything else at this status means Design QC still owes a verdict. (An Arka that
        # QC has passed but the Head has not sits at `awaiting_head_arka` and never
        # reaches this branch.) Same status, opposite bottleneck.
        if current_arka is not None and current_arka.head_verdict == ARKA_APPROVED:
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
    # PART 4.6 — the Head's triage queue. One more batched read, on the same `__in` shape
    # as the four above, so the fixed query budget is kept. Ordered oldest first here so
    # both the queue panel and the attention band inherit it without re-sorting.
    pending_crs = list(
        DesignChangeRequest.objects
        .filter(attempt__assignment_id__in=assignment_ids,
                verdict=CHANGE_REQUEST_PENDING)
        .select_related('attempt', 'requested_by__user')
        .order_by('requested_at')
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

    # `cr.attempt` is select_related, so reading assignment_id off it costs no query.
    pending_cr_by_assignment = {}
    for cr in pending_crs:
        pending_cr_by_assignment.setdefault(cr.attempt.assignment_id, []).append(cr)

    arka_by_attempt = {k.attempt_id: k for k in arkas}
    current_arka = {a.pk: arka_by_attempt.get(current_attempt_id.get(a.pk))
                    for a in assignments}

    commitments_by_assignment = {}
    for c in commitments:
        commitments_by_assignment.setdefault(c.assignment_id, []).append(c)
    # PART 8: the APPROVED date drives every number on this page, and a pending
    # extension is carried alongside it rather than replacing it. See the
    # effective/pending note at the top of this module.
    effective_by_assignment = {}
    pending_by_assignment   = {}
    for a in assignments:
        rows = commitments_by_assignment.get(a.pk, [])
        effective_by_assignment[a.pk] = effective_commitment(rows)
        pending_by_assignment[a.pk]   = pending_extension(rows)

    # ---- per-site facts every panel reuses --------------------------------------
    sites = []
    for a in assignments:
        arka = current_arka.get(a.pk)
        commitment = effective_by_assignment.get(a.pk)
        pending    = pending_by_assignment.get(a.pk)
        # PART 9: "approved" for capacity purposes means BOTH gates passed. A capacity
        # only Design QC has signed off is not a number the tender may be reported on.
        approved_arka = arka if (arka is not None
                                 and arka.head_verdict == ARKA_APPROVED) else None
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
            # The extension awaiting a verdict, if any. Rendered as a chip beside the
            # due date; it deliberately does NOT feed `overdue` or `commitment`.
            'pending_extension': pending,
            'has_approved_due': bool(commitment and commitment.approved_at),
            'overdue':       overdue,
            'days_over':     days_overdue(commitment, today) if overdue else 0,
            'blocked':       a.status == DESIGN_SURVEY_RETURNED,
            'revisions':     max(len(commitments_by_assignment.get(a.pk, [])) - 1, 0),
            'attempts':      attempts_by_assignment.get(a.pk, []),
            'released':      a.status == DESIGN_RELEASED,
            # PART 4.6 — the oldest untriaged PM change request on this site, or None.
            # A list is carried rather than a bool because the queue panel needs the row
            # itself and the attention band needs its age.
            'pending_crs':   pending_cr_by_assignment.get(a.pk, []),
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
        # PART 4.6 — the Head's own triage queue, oldest first. Not passed to the Design
        # QC dashboard: deciding whether the brief may move is the Head's, not QC's.
        'change_requests': change_request_queue(sites, today),
        # PART 9 — the Design QC counterparts of the two panels above. Computed here
        # rather than in a second entry point so the QC dashboard is genuinely a SUBSET of
        # this one: same batched reads, same `sites` list, same functions with different
        # stage tuples. The Head's dashboard does not render these; the QC dashboard picks
        # these two and leaves `workload` and `capacity` behind entirely.
        'qc_queue':      qc_review_queue_age(sites, today),
        'qc_attention':  attention_list(sites, today, own_stages=QC_ACTION_STAGES,
                                        own_only=True),
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


# ---------------------------------------------------------------------------
# PART 9 — WHAT CAUSED AN ATTEMPT TO EXIST
# ---------------------------------------------------------------------------
# THE CAUSE OF ATTEMPT N+1 IS RECORDED ON ATTEMPT N, and that is the one thing to
# understand before reading the rework numbers.
#
# `opened_reason` says WHICH LOOP opened an attempt (a QC failure, or a PM change
# request). It does not say whose fault the loop was — that lives in the failure category
# on the attempt that FAILED, one row earlier. So classifying attempt N+1 means reading
# attempt N's category and asking error_category_group() which group it falls in.
#
# This costs NO extra query. `tender_metrics()` already loads every attempt for the tender
# in one batched read, and the walk below is over that list.

CAUSE_PM_CHANGE   = 'pm'      # the brief moved
CAUSE_UNCATEGORISED = 'legacy'  # a pre-Part-9 QC failure, which carries no category


def classify_attempt_causes(attempts):
    """Map {attempt_number: cause} over one assignment's attempts.

    Cause is one of:
        None                  the initial attempt — not rework at all
        'A' / 'B' / 'C'       opened by a failure in that error group
        CAUSE_PM_CHANGE       opened by a PM change request
        CAUSE_UNCATEGORISED   opened by a QC failure that recorded no category

    CAUSE_UNCATEGORISED IS REPORTED, NOT DISCARDED. Every attempt opened by a QC failure
    before Part 9 has no category, because the field did not exist. Silently dropping
    those would quietly shrink every historical designer's rework multiplier the day this
    ships, which is the most misleading thing this module could do. They are counted into
    the designer's figure — before Part 9 a QC failure had no other meaning, since a bad
    survey went through the Design Hold path and a moved brief through a change request —
    and the count is surfaced separately so the mixture is visible rather than implied.

    The category is read from the Head's field first, then Design QC's. Exactly one of the
    two is ever populated on a given attempt: if Design QC failed it the Head never saw
    it, and if the Head failed it Design QC had already passed it.
    """
    ordered   = sorted(attempts, key=lambda t: t.attempt_number)
    by_number = {t.attempt_number: t for t in ordered}
    causes    = {}
    for t in ordered:
        if t.opened_reason == ATTEMPT_REASON_PM_CHANGE_REQUEST:
            causes[t.attempt_number] = CAUSE_PM_CHANGE
        elif t.opened_reason == ATTEMPT_REASON_QC_FAILED:
            previous = by_number.get(t.attempt_number - 1)
            category = ''
            if previous is not None:
                category = (previous.head_failure_category
                            or previous.qc_failure_category)
            causes[t.attempt_number] = (error_category_group(category)
                                        or CAUSE_UNCATEGORISED)
        else:
            causes[t.attempt_number] = None
    return causes


def designer_workload(sites, today=None):
    """One row per designer holding at least one assignment on this tender.

    SITES AND kW SIT SIDE BY SIDE ON PURPOSE. Sites are not equal units of work;
    allocating by count alone hands one designer six ground mounts and another six
    rooftops and calls them balanced.

    REWORK IS SPLIT AND NEVER MERGED. Part 5 split it two ways; PART 9 SPLITS IT THREE,
    and the three are reported as separate figures that must not be added together:

        rework           attempts caused by a GROUP A failure — the design was wrong.
                         This is the designer's number, and the only one that should
                         ever drive coaching.
        input_quality    attempts caused by a GROUP B or C failure — the survey was
                         wrong, or the brief moved after work started. NOT the
                         designer's error (settled decision 8). A team with a high
                         figure here needs better surveys or a pinned-down customer;
                         coaching the designer would be useless and unfair.
        pm_change_request  attempts opened by a formal PM change request, exactly as in
                         Part 5 and deliberately unchanged.

    Both multipliers are ÷ released sites — undefined with no released sites, and reported
    as None (rendered '—') rather than 0, which would read as "no rework" when it means
    "no data".

    THE DENOMINATOR IS THE SAME FOR BOTH so the two are comparable at a glance. Only the
    numerator differs, and Group B and C attempts are excluded from `rework` — that
    exclusion is the whole point of the change.
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
            # Part 9 — the three-way split of what those attempts were caused BY.
            'designer_error_attempts': 0,
            'input_problem_attempts':  0,
            'uncategorised_attempts':  0,
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
        causes = classify_attempt_causes(s['attempts'])
        for t in s['attempts']:
            if t.opened_reason == ATTEMPT_REASON_QC_FAILED:
                row['qc_failed'] += 1
            elif t.opened_reason == ATTEMPT_REASON_PM_CHANGE_REQUEST:
                row['pm_change_request'] += 1

            cause = causes.get(t.attempt_number)
            if cause == ERROR_GROUP_A:
                row['designer_error_attempts'] += 1
            elif cause in ('B', 'C'):
                row['input_problem_attempts'] += 1
            elif cause == CAUSE_UNCATEGORISED:
                row['uncategorised_attempts'] += 1

    rows = []
    for row in by_designer.values():
        released = row['released']
        # THE PART 9 EXCLUSION, and the only change to how this number is computed:
        # attempts opened by a Group B or C failure come out of the numerator entirely.
        # Everything else Part 5 counted is still counted.
        designer_attempts = row['attempts'] - row['input_problem_attempts']
        row['rework'] = (round(designer_attempts / released, 1) if released else None)
        row['input_quality'] = (round(row['input_problem_attempts'] / released, 1)
                                if released else None)
        # Reported as its own figure rather than folded into either multiplier — a PM
        # change request is neither a design error nor an input problem.
        row['pm_change_multiplier'] = (round(row['pm_change_request'] / released, 1)
                                       if released else None)
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


def _queue_age(sites, arka_stage, package_stage, today=None):
    """How long ONE reviewer's queue has been waiting. Shared by both gates.

    PART 9 MADE THIS PARAMETRIC RATHER THAN COPYING IT. The arithmetic is identical for
    Design QC and the Design Head; only which two stages count as "mine" differs. Two
    copies would drift the moment one of them gained a special case, and the whole reason
    the Part 9 dashboard is a subset of the Part 5 one is that they share this code.

    Ages are measured from the moment the item entered THIS queue. For an Arka that is its
    own `submitted_at`; for a package it is `updated_at` on the assignment, which is when
    it last changed status — deliberately not `qc_started_at`, because an item waiting for
    a review has not started it yet.
    """
    today = today or timezone.localdate()

    pending_arka = [s for s in sites if s['stage'] == arka_stage and s['arka']]
    awaiting_pkg = [s for s in sites if s['stage'] == package_stage]

    def _age_days(dt):
        return (today - timezone.localtime(dt).date()).days if dt else 0

    oldest_arka = min(pending_arka, key=lambda s: s['arka'].submitted_at, default=None)
    oldest_pkg  = min(awaiting_pkg, key=lambda s: s['assignment'].updated_at, default=None)

    return {
        'arka_count':      len(pending_arka),
        'arka_oldest_days': _age_days(oldest_arka['arka'].submitted_at) if oldest_arka else None,
        'arka_oldest_site': oldest_arka['project'] if oldest_arka else None,
        'qc_count':        len(awaiting_pkg),
        'qc_oldest_days':  _age_days(oldest_pkg['assignment'].updated_at) if oldest_pkg else None,
        'qc_oldest_site':  oldest_pkg['project'] if oldest_pkg else None,
    }


def review_queue_age(sites, today=None):
    """The DESIGN HEAD's own queue: Arkas and packages Design QC has passed up to him.

    PART 9 REPOINTED THIS. In Part 5 the Head was the single approver and this counted
    `arka_submitted` / `artifacts_uploaded`. Those are now Design QC's queue; the Head's
    is what QC has passed. Keeping the old stages here would have shown him a backlog he
    cannot clear and hidden the one he can.
    """
    return _queue_age(sites, 'awaiting_head_arka', 'awaiting_head_qc', today)


def qc_review_queue_age(sites, today=None):
    """DESIGN QC's own queue: Arkas awaiting a first verdict, packages awaiting review.

    The gate-1 counterpart of review_queue_age(), and the same function underneath.
    """
    return _queue_age(sites, 'arka_submitted', 'artifacts_uploaded', today)


def change_request_queue(sites, today=None):
    """PART 4.6 — every PM change request awaiting the Design Head, oldest first.

    OLDEST FIRST AND NOT CAPPED. This is not a "top ten worst" panel like the attention
    list; it is a queue the Head must empty. Every row in it is a suspended review and a
    designer who cannot be given a verdict, so hiding the eleventh would hide exactly the
    one that has been waiting longest to be noticed.

    `age_days` is from `requested_at`, which is when the suspension started — not from
    `assignment.updated_at`, which moves on any save and would understate the wait.
    """
    today = today or timezone.localdate()
    rows = []
    for s in sites:
        for cr in s['pending_crs']:
            rows.append({
                'change_request': cr,
                'project':        s['project'],
                'designer':       s['designer'],
                'requested_by':   cr.requested_by,
                'reason':         cr.reason,
                'attempt_number': cr.attempt.attempt_number,
                'requested_at':   cr.requested_at,
                'age_days':       (today - timezone.localtime(cr.requested_at).date()).days,
                'stage':          STAGE_LABELS.get(s['stage'], s['stage']),
            })
    rows.sort(key=lambda r: r['requested_at'])
    return rows


# Severity bands for the attention list. Lower sorts first.
#
# PART 4.6 INSERTED A BAND at position 1 rather than appending one. An untriaged change
# request is second only to a missed date: it has stopped a review, and unlike a Design
# Hold the person who can clear it is the person reading this list. The relative order of
# every pre-existing band is unchanged.
_SEV_OVERDUE  = 0
_SEV_CHANGE   = 1
_SEV_BLOCKED  = 2
_SEV_REVISED  = 3
_SEV_HEAD     = 4

ATTENTION_LIMIT = 10


def attention_list(sites, today=None, limit=ATTENTION_LIMIT,
                   own_stages=HEAD_ACTION_STAGES, own_only=False):
    """At most ten rows, worst first. A work queue, not a report (settled decision 8).

    Ordering is by severity band, then by the magnitude that makes a row urgent within
    its band — days overdue, then days blocked, then revision count, then queue age. A
    site can qualify on several grounds; it appears ONCE, under its worst.

    PART 9 PARAMETRISED THE LAST BAND rather than forking the function.

      `own_stages`  which stages count as "waiting on you". Defaults to the Head's, so
                    every Part 5 caller is unchanged; the Design QC dashboard passes
                    QC_ACTION_STAGES.
      `own_only`    when True, ONLY that last band is produced — no overdue, no Design
                    Hold, no due-date revisions. Design QC's dashboard sets this: those
                    three bands are about how the tender is being RUN, which is the Head's
                    remit, and putting them on a reviewer's screen would hand them a list
                    of things they have no authority to fix.
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
            # PART 9: the KEY as well as the label, because the two artifacts live on two
            # different screens. An Arka-stage row has to link to design_head_review; the
            # package screen would tell the reviewer there is nothing to review yet, which
            # is true of the package and useless to somebody holding an Arka verdict.
            'stage_key': site['stage'],
        })

    if not own_only:
        for s in sorted((x for x in sites if x['overdue']),
                        key=lambda x: x['days_over'], reverse=True):
            attempt_no = s['assignment'].current_attempt_number or 1
            _add(s, _SEV_OVERDUE, s['days_over'],
                 f'{s["days_over"]} day(s) past the agreed date, on attempt {attempt_no}',
                 'danger')

        # PART 4.6 — an untriaged change request. Nothing else on any screen makes one
        # visible, and while it sits there a review is suspended and a designer is
        # waiting on a verdict nobody may record. Excluded from the `own_only` list on
        # purpose: triage is the Head's, and Design QC cannot clear this row.
        for s in sorted((x for x in sites if x['pending_crs']),
                        key=lambda x: x['pending_crs'][0].requested_at):
            cr = s['pending_crs'][0]
            days = (today - timezone.localtime(cr.requested_at).date()).days
            _add(s, _SEV_CHANGE, days,
                 f'PM change request awaiting your decision — {days} day(s), '
                 f'attempt {cr.attempt.attempt_number}', 'danger')

        for s in sites:
            if not s['blocked']:
                continue
            started = s['assignment'].survey_returned_at
            days = (today - timezone.localtime(started).date()).days if started else 0
            reason = (s['assignment'].survey_return_reason or '').strip() or 'no reason recorded'
            _add(s, _SEV_BLOCKED, days, f'Design Hold {days} day(s) — {reason}', 'warning')

        for s in sorted((x for x in sites if x['revisions'] >= 3 and not x['released']),
                        key=lambda x: x['revisions'], reverse=True):
            _add(s, _SEV_REVISED, s['revisions'],
                 f'Due date revised {s["revisions"]} times', 'warning')

    # Longest-waiting items in the VIEWER's own queue, so their backlog is on the same
    # list as the designers' — a reviewer is a bottleneck like any other.
    #
    # An Arka's wait is dated from ITS OWN submitted_at, not from assignment.updated_at,
    # so this agrees with the queue-age panel. They diverge in practice: updated_at moves
    # on any assignment save, while submitted_at is when the item actually entered the
    # queue, and two panels reporting different ages for the same item is worse than
    # either number being slightly off.
    head_waiting = [s for s in sites if s['stage'] in own_stages]

    def _waiting_since(site):
        # Both Arka stages date from the submission itself. `awaiting_head_arka` is the
        # same physical Arka that `arka_submitted` was, handed on rather than resubmitted,
        # so its clock does not restart when Design QC passes it — the designer has been
        # waiting since they submitted, whoever currently holds it.
        if site['stage'] in ('arka_submitted', 'awaiting_head_arka') and site['arka'] is not None:
            return site['arka'].submitted_at
        return site['assignment'].updated_at

    for s in sorted(head_waiting, key=_waiting_since):
        days = (today - timezone.localtime(_waiting_since(s)).date()).days
        _add(s, _SEV_HEAD, days,
             f'Waiting on you — {STAGE_LABELS.get(s["stage"], s["stage"]).lower()}, '
             f'{days} day(s)', 'secondary')

    rows.sort(key=lambda r: (r['severity'], -r['magnitude']))
    return rows[:limit]
