"""
Quality analytics for the Design Head (Part 10).

NOTHING IN THIS MODULE WRITES, and unlike design_metrics.py that is not only a
convention here — it is the whole reason the screen is defensible. Every figure below is
arithmetic over rows some other part created. The single write in this session is the
Head's own metric selection, and it lives in the view, not here.

WHY A CORE SET IS ALWAYS ON
---------------------------
The Head configures this page freely except for five metrics that cannot be switched off.
That constraint is not paternalism about dashboards; it is what keeps the underlying data
honest.

A dashboard that measures only designer error rates is surveillance, and a design team
reads it that way. The predictable response is pressure on the Design QC reviewer to log
a failure as an informal correction, and issues fixed by a phone call before anybody
records them. Both destroy exactly the data the page exists to show. So the locked set
deliberately spans all four groups and includes metrics that measure the PROCESS and the
REVIEWERS, not only the designers:

    first_pass_rate       designer execution        (A)
    error_distribution    where errors cluster      (A)  — the training signal
    hold_rate             survey quality            (B)  — not the designer's fault
    change_request_rate   brief stability, per PM   (C)  — not the designer's fault
    overturn_rate         reviewer consistency      (D)  — measures the gates themselves

Three of those five cannot make a designer look bad. That is the point.

THE MINIMUM DENOMINATOR IS NOT COSMETIC
---------------------------------------
Every rate carries its denominator, and below 5 it does not render as a number at all —
`Insufficient data (n=3)`, never a percentage, never a ratio. On a small tender a designer
with one failure in two released sites reads as a 50% failure rate, and that figure is
noise that will be repeated in a review as fact. rate() and ratio() are the only two ways
a number leaves this module, so the rule cannot be forgotten at one call site.

GROUP MEMBERSHIP IS NEVER HARDCODED HERE
----------------------------------------
Every A/B/C decision goes through models.error_category_group(). There is no category
tuple in this file, and there must not be one: a second copy would silently move rework
between a designer's column and the input-quality column and nothing would fail.

QUERY SHAPE
-----------
`analytics_dataset()` issues a fixed set of batched reads regardless of how many sites or
tenders are in scope — the assignments, their attempts, every Arka version, the due-date
commitments, the change requests, and (only when the Design Hold duration metric is on)
the hold events. Everything after that is Python over those lists. A 200-site tender costs
the same as a 5-site one, and the all-tenders view costs the same as one tender.

WHAT THIS MODULE CANNOT COMPUTE, AND WHY NO FIELD WAS ADDED
-----------------------------------------------------------
Four metrics in the catalogue are reduced rather than dropped, and each says so on screen:

  stage_dwell     There is NO per-status entry timestamp anywhere in the design module.
                  "Where time is spent" per status is not answerable. What IS answerable
                  is the set of intervals the terminal stamps define, and that is what
                  ships. Adding a status-entry stamp is a schema change to a workflow
                  model; the rule for this session is to report it, not to add it.
  cr_by_stage     DesignChangeRequest does not store the status at the moment it was
                  raised. The stage is DERIVED by comparing requested_at against that
                  attempt's own gate stamps, which is honest but approximate; the
                  attempt-number distribution beside it is exact.
  hold_duration   DesignAssignment carries ONE survey_returned_at triple, overwritten on
                  each hold, so the model alone gives the latest interval only. The full
                  history is recoverable from paired ActivityLog action codes, which is
                  what this module reads — best-effort, because log_activity() swallows
                  its own exceptions, and open holds are excluded rather than measured
                  against today.
  capacity_throughput
                  "over the period" has nothing to bind to: there is no period control in
                  this screen and no period field. It is computed over every released site
                  in scope and labelled that way.
"""
from decimal import Decimal
from statistics import median

from .models import (
    ActivityLog, ArkaSubmission, DesignAssignment, DesignAttempt,
    DesignChangeRequest, DueDateCommitment,
    ARKA_APPROVED, ARKA_PENDING,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_REJECTED,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    ERROR_GROUP_A, ERROR_GROUP_B, ERROR_GROUP_C,
    DESIGN_ERROR_CATEGORY_LABELS, error_category_group,
    QC_FAILED, QC_PENDING,
)
from .design_metrics import (
    CAUSE_PM_CHANGE, CAUSE_UNCATEGORISED, classify_attempt_causes,
    effective_commitment,
)
from .utils import is_working_day


# ---------------------------------------------------------------------------
# 1. The sample size guard
# ---------------------------------------------------------------------------
# THE ONLY TWO WAYS A NUMBER LEAVES THIS MODULE. Both take a denominator and both refuse
# to produce a figure below MIN_DENOMINATOR, so there is no call site that can skip the
# rule by computing `a / b` inline.

#: Below this, no rate and no ratio is displayed. Settled decision 1; not configurable.
MIN_DENOMINATOR = 5
#: At or below this, the figure renders with a visible low-confidence marker.
LOW_CONFIDENCE_MAX = 14

STATE_INSUFFICIENT = 'insufficient'
STATE_LOW          = 'low'
STATE_OK           = 'ok'


def _state(denominator):
    if denominator < MIN_DENOMINATOR:
        return STATE_INSUFFICIENT
    if denominator <= LOW_CONFIDENCE_MAX:
        return STATE_LOW
    return STATE_OK


def rate(numerator, denominator):
    """A PERCENTAGE with its denominator, or an explicit refusal to show one.

    `value` is None whenever the figure must not be displayed, so a template that forgets
    the state check renders nothing rather than a misleading number. n=0 is `insufficient`
    like any other denominator under 5 — "no data" and "too little data" are the same
    answer to the reader, and neither is a percentage.
    """
    denominator = int(denominator or 0)
    state = _state(denominator)
    return {
        'n':       denominator,
        'numerator': int(numerator or 0),
        'state':   state,
        'value':   (round(100.0 * numerator / denominator, 1)
                    if state != STATE_INSUFFICIENT else None),
        'is_pct':  True,
    }


def ratio(numerator, denominator, places=2):
    """A RATIO (attempts per site, versions per attempt) under the same guard.

    Separate from rate() only so the template knows not to append a % sign. The refusal
    threshold is identical and deliberately so — settled decision 1 says every rate
    metric, and a ratio over a denominator of two is exactly as noisy as a percentage
    over a denominator of two.
    """
    denominator = int(denominator or 0)
    state = _state(denominator)
    return {
        'n':       denominator,
        'numerator': round(float(numerator or 0), places),
        'state':   state,
        'value':   (round(float(numerator) / denominator, places)
                    if state != STATE_INSUFFICIENT else None),
        'is_pct':  False,
    }


# ---------------------------------------------------------------------------
# 2. The catalogue
# ---------------------------------------------------------------------------
# FOUR GROUPS, and the group is what makes the page a process tool rather than a scoreboard.
# `core=True` means always on and not offerable in the selector as a checkbox — the
# template renders it locked, with `why` shown beside it so the Head can see the reason
# rather than being told there is one.
#
# `caveat` is not decoration. Four metrics are reduced against what the catalogue asked
# for (see the module docstring), and the reduction is printed next to the figure. A
# number whose limitation lives only in a code comment will be read as if it had none.

GROUP_A = 'A'
GROUP_B = 'B'
GROUP_C = 'C'
GROUP_D = 'D'

METRIC_GROUPS = [
    (GROUP_A, 'Designer execution',
     'What the design team produced. Group A causes only — a bad survey or a moved brief '
     'never appears here.'),
    (GROUP_B, 'Input quality',
     'Problems that arrived with the site, not with the design. Never folded into any '
     'designer figure.'),
    (GROUP_C, 'Brief stability',
     'How much the requirement moved after work started, counted against the PM who '
     'moved it.'),
    (GROUP_D, 'Process and reviewer consistency',
     'Whether the workflow and the two gates are doing their job.'),
]


class Metric:
    """One entry in the catalogue. A plain descriptor — it holds no data and does no
    arithmetic; `compute()` below dispatches on `key`."""

    def __init__(self, key, group, label, description, core=False, why='', caveat=''):
        self.key = key
        self.group = group
        self.label = label
        self.description = description
        self.core = core
        self.why = why
        self.caveat = caveat


METRIC_CATALOGUE = [
    # ── A — designer execution ───────────────────────────────────────────────
    Metric('first_pass_rate', GROUP_A, 'First-pass rate',
           'Released sites that were released on their first attempt, per designer and '
           'team-wide.',
           core=True,
           why='Locked: it is the one designer metric that measures work getting through '
               'cleanly rather than counting failures, so the page cannot be read as a '
               'failure tally alone.'),
    Metric('rework_multiplier', GROUP_A, 'Rework multiplier',
           'Attempts per released site, counting only attempts a Group A failure caused. '
           'Group B, Group C and PM-change attempts are excluded from the numerator.',
           caveat='Stricter than the rework column on the tender dashboard, which also '
                  'counts attempts opened by a PM change request. The two figures will '
                  'differ and both are correct for what they name.'),
    Metric('arka_iterations', GROUP_A, 'Arka iterations',
           'Average Arka versions per attempt, per designer. Versions carried forward '
           'from an earlier attempt are excluded — nobody iterated on those.'),
    Metric('qc_failure_rate', GROUP_A, 'QC failure rate',
           'Attempts failed at the Design QC gate, over attempts Design QC ruled on.'),
    Metric('head_failure_rate', GROUP_A, 'Head failure rate',
           'Attempts failed at the Design Head gate, over attempts that reached it.'),
    Metric('error_distribution', GROUP_A, 'Error category distribution',
           'Count by Group A category, team-wide and per designer, across both package '
           'failures and Arka rejections.',
           core=True,
           why='Locked: this is the training signal. It shows WHERE errors cluster, which '
               'is the only part of the page that says what to do next rather than who '
               'was slow.'),
    Metric('capacity_throughput', GROUP_A, 'Capacity throughput',
           'kW released per designer, from the approved Arka on each released site.',
           caveat='Over every released site in scope. There is no period control on this '
                  'screen and no period field to bind one to.'),

    # ── B — input quality ────────────────────────────────────────────────────
    Metric('hold_rate', GROUP_B, 'Design Hold rate',
           'Sites ever placed on Design Hold over an inadequate survey, over sites '
           'allocated. Team-wide — this measures the survey, not a designer.',
           core=True,
           why='Locked: without it every survey failure lands silently in a designer\'s '
               'rework figure and the page stops being able to tell the two apart.'),
    Metric('group_b_failures', GROUP_B, 'Group B failure count',
           'Failures categorised as an input problem — the survey was inadequate, or the '
           'site differed from it. Counted here and nowhere else.'),
    Metric('hold_duration', GROUP_B, 'Design Hold duration',
           'Average days a site spent on Design Hold, over completed holds.',
           caveat='Reconstructed from paired activity-log events, because the assignment '
                  'row keeps only the most recent hold. Best-effort: a lost log row is '
                  'silently absent, and holds still open are excluded rather than '
                  'measured against today.'),

    # ── C — brief stability ──────────────────────────────────────────────────
    Metric('change_request_rate', GROUP_C, 'Change request rate',
           'Accepted change requests over released sites, per REQUESTING PM.',
           core=True,
           why='Locked: an accepted change request is rework the designer did not cause. '
               'Counting it against the PM who raised it is what stops it being counted '
               'against the designer by default.'),
    Metric('cr_rejection_rate', GROUP_C, 'Change request rejection rate',
           'Rejected change requests over all requests raised. A rate near zero means '
           'the Head\'s triage is a rubber stamp, not a gate.'),
    Metric('group_c_failures', GROUP_C, 'Group C failure count',
           'Failures categorised as a brief change — the requirement changed, or the '
           'scope was revised after design started.'),
    Metric('cr_by_stage', GROUP_C, 'Change requests by stage',
           'How late in the process the brief moved.',
           caveat='The status at the moment a request was raised is not stored. The stage '
                  'is derived by comparing the request time against that attempt\'s own '
                  'gate stamps; the attempt-number split beside it is exact.'),

    # ── D — process and reviewer consistency ─────────────────────────────────
    Metric('overturn_rate', GROUP_D, 'Overturn rate',
           'Artifacts where the Head\'s verdict differed from Design QC\'s, over '
           'artifacts that reached the Head gate — per QC reviewer and overall.',
           core=True,
           why='Locked: it is the only metric that measures the reviewers. If it stays '
               'near zero across two tenders the Head\'s gate is a formality and the '
               'second gate is not earning its cost.'),
    Metric('on_time_delivery', GROUP_D, 'On-time delivery',
           'Sites released on or before the APPROVED due date, per designer. A pending '
           'extension request never moves the date this is measured against.'),
    Metric('extension_rate', GROUP_D, 'Due date extension rate',
           'Assignments carrying more than one due-date commitment, over assignments '
           'with any commitment at all.'),
    Metric('cycle_time', GROUP_D, 'Cycle time',
           'Working days from allocation to release — median, and the range around it.'),
    Metric('stage_dwell', GROUP_D, 'Stage dwell time',
           'Median working days across the intervals the workflow actually timestamps.',
           caveat='NOT time-per-status. The design module stamps no status entry, so '
                  'per-status dwell is not answerable from existing data. These are the '
                  'intervals the terminal stamps define, named individually.'),
    Metric('queue_latency', GROUP_D, 'Review queue latency',
           'Average days an item waited for a Design QC verdict, and for a Head verdict, '
           'split by Arka and package.'),
]

METRICS_BY_KEY = {m.key: m for m in METRIC_CATALOGUE}

#: Always on. Cannot be switched off, and never stored in a preference row.
CORE_METRICS = tuple(m.key for m in METRIC_CATALOGUE if m.core)
#: Everything the Head may switch on. The whitelist the configuration POST validates against.
OPTIONAL_METRICS = tuple(m.key for m in METRIC_CATALOGUE if not m.core)


def selected_metric_keys(preference):
    """The full set of metric keys to render, given a preference row or None.

    CORE IS UNIONED IN HERE, not read from storage, so a core metric is on even if the
    row is empty, corrupt, or was written before that metric existed. Unrecognised keys
    and any core key that somehow reached storage are dropped rather than raising —
    renaming a metric in a later part must degrade to "it comes back off", not to a 500.
    """
    stored = []
    if preference is not None and isinstance(preference.metrics, list):
        stored = [k for k in preference.metrics if k in OPTIONAL_METRICS]
    return set(CORE_METRICS) | set(stored)


def catalogue_for_display(selected):
    """The catalogue grouped for the selector, with each metric's on/locked state."""
    out = []
    for key, label, blurb in METRIC_GROUPS:
        out.append({
            'key': key, 'label': label, 'blurb': blurb,
            'metrics': [
                {'metric': m, 'on': m.key in selected, 'locked': m.core}
                for m in METRIC_CATALOGUE if m.group == key
            ],
        })
    return out


# ---------------------------------------------------------------------------
# 3. The batched reads
# ---------------------------------------------------------------------------

def analytics_dataset(programs, need_hold_events=False):
    """Every row the whole catalogue needs, in six reads (seven with hold events).

    `programs` is a list of Program rows — one for the per-tender view, all OPEX tenders
    for the combined view. The two cost the same: every read below is a single `__in`,
    and nothing in this module queries per site, per designer or per attempt.

    ARKA VERSIONS ARE LOADED IN FULL, not filtered to `is_current` as design_metrics does.
    The iteration count is the number of versions, so filtering to the live one would make
    every attempt look like a single-version attempt.
    """
    program_ids = [p.pk for p in programs]

    assignments = list(
        DesignAssignment.objects
        .filter(project__program_id__in=program_ids, project__is_deleted=False)
        .select_related('project', 'project__program',
                        'assigned_to__user', 'project__assigned_pm__user')
        .order_by('project__project_id')
    )
    assignment_ids = [a.pk for a in assignments]

    attempts = list(
        DesignAttempt.objects
        .filter(assignment_id__in=assignment_ids)
        .select_related('qc_reviewed_by__user', 'head_reviewed_by__user')
    )
    attempt_ids = [t.pk for t in attempts]

    arkas = list(
        ArkaSubmission.objects
        .filter(attempt_id__in=attempt_ids)
        .select_related('reviewed_by__user')
    )
    commitments = list(
        DueDateCommitment.objects.filter(assignment_id__in=assignment_ids)
    )
    change_requests = list(
        DesignChangeRequest.objects
        .filter(attempt_id__in=attempt_ids)
        .select_related('requested_by__user', 'attempt')
        .order_by('requested_at')
    )

    # Only paid for when the Design Hold duration metric is switched on. It is the one
    # read in this module that is not needed by anything core.
    hold_events = []
    if need_hold_events:
        hold_events = list(
            ActivityLog.objects
            .filter(project_id__in=[a.project_id for a in assignments],
                    action_code__in=('design_blocked', 'design_survey_unblocked'))
            .values_list('project_id', 'action_code', 'timestamp')
            .order_by('project_id', 'timestamp')
        )

    # ---- index in Python; no further queries --------------------------------
    attempts_by_assignment = {}
    for t in attempts:
        attempts_by_assignment.setdefault(t.assignment_id, []).append(t)

    arkas_by_attempt = {}
    for k in arkas:
        arkas_by_attempt.setdefault(k.attempt_id, []).append(k)

    commitments_by_assignment = {}
    for c in commitments:
        commitments_by_assignment.setdefault(c.assignment_id, []).append(c)

    attempt_by_id = {t.pk: t for t in attempts}
    assignment_by_id = {a.pk: a for a in assignments}

    sites = []
    for a in assignments:
        own_attempts = sorted(attempts_by_assignment.get(a.pk, []),
                              key=lambda t: t.attempt_number)
        final = next((t for t in own_attempts
                      if t.attempt_number == a.current_attempt_number), None)
        # BOTH GATES, exactly as design_metrics.capacity_panel requires: a capacity only
        # Design QC has signed off is not a released capacity.
        approved_arka = None
        if final is not None:
            for k in sorted(arkas_by_attempt.get(final.pk, []), key=lambda x: x.version):
                if k.is_current and k.head_verdict == ARKA_APPROVED:
                    approved_arka = k
        sites.append({
            'assignment':  a,
            'project':     a.project,
            'program':     a.project.program,
            'designer':    a.assigned_to,
            'pm':          a.project.assigned_pm,
            'attempts':    own_attempts,
            'final_attempt': final,
            'approved_arka': approved_arka,
            'capacity_kw': approved_arka.capacity_kw if approved_arka else None,
            'commitments': commitments_by_assignment.get(a.pk, []),
            'released':    a.status == DESIGN_RELEASED,
            'ever_held':   a.survey_returned_at is not None,
            'allocated':   a.assigned_at is not None,
        })

    return {
        'programs':        programs,
        'sites':           sites,
        'assignments':     assignments,
        'attempts':        attempts,
        'arkas':           arkas,
        'arkas_by_attempt': arkas_by_attempt,
        'attempt_by_id':   attempt_by_id,
        'assignment_by_id': assignment_by_id,
        'commitments':     commitments,
        'change_requests': change_requests,
        'hold_events':     hold_events,
    }


# ---------------------------------------------------------------------------
# 4. Shared helpers
# ---------------------------------------------------------------------------

def _person_label(profile):
    if profile is None:
        return 'Unassigned'
    return profile.user.get_full_name() or profile.user.username


def _person_rows(bucket, build):
    """Turn a {profile_pk: accumulator} dict into a sorted list of display rows.

    Sorted by NAME, never by the metric. Settled decision: no leaderboard and no ranked
    designer list — ordering the table by the number is the leaderboard, whatever the
    column header says.
    """
    rows = [build(v) for v in bucket.values()]
    rows.sort(key=lambda r: r['label'].lower())
    return rows


def _bucket(store, profile, extra=None):
    key = profile.pk if profile is not None else None
    row = store.get(key)
    if row is None:
        row = {'profile': profile, 'label': _person_label(profile)}
        if extra:
            row.update({k: (v() if callable(v) else v) for k, v in extra.items()})
        store[key] = row
    return row


def _working_days(start, end):
    """Working days between two dates under the company calendar (utils.is_working_day).

    Counts the days strictly after `start` up to and including `end`, so same-day is 0
    rather than 1. Returns None if either end is missing or the interval runs backwards —
    a negative duration is corrupt data, not a fast turnaround, and averaging it in would
    quietly pull every figure down.
    """
    if start is None or end is None:
        return None
    if end < start:
        return None
    days, cursor = 0, start
    while cursor < end:
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if is_working_day(cursor):
            days += 1
    return days


def _localdate(dt):
    from django.utils import timezone
    return timezone.localtime(dt).date() if dt else None


def _spread(values):
    """Median with the range around it. Empty input returns an explicit no-data shape
    rather than zeros, which would read as "instant" instead of "nothing to measure"."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {'n': 0, 'median': None, 'min': None, 'max': None,
                'state': STATE_INSUFFICIENT}
    return {
        'n': len(vals),
        'median': round(median(vals), 1),
        'min': vals[0],
        'max': vals[-1],
        # Spreads are descriptive, not rates, so they show at any n — but the marker
        # still appears below MIN_DENOMINATOR so a median of three is not read as typical.
        'state': _state(len(vals)),
    }


def _failure_rows(data):
    """Every recorded failure category in scope, as (group, category, designer, source).

    ONE PLACE THE FOUR CATEGORY FIELDS ARE READ. There are four: the QC and Head fields on
    DesignAttempt (a failed package) and the same pair on ArkaSubmission (a rejected Arka
    version). Any metric that counts failures reads this list, so a metric cannot
    accidentally count packages and forget Arkas, or vice versa.

    The group comes from error_category_group() on every row without exception.
    """
    rows = []
    for t in data['attempts']:
        designer = data['assignment_by_id'][t.assignment_id].assigned_to
        for category, gate in ((t.qc_failure_category, 'Design QC'),
                               (t.head_failure_category, 'Design Head')):
            if not category:
                continue
            rows.append({
                'group':    error_category_group(category),
                'category': category,
                'label':    DESIGN_ERROR_CATEGORY_LABELS.get(category, category),
                'designer': designer,
                'gate':     gate,
                'source':   'Package',
            })
    for k in data['arkas']:
        attempt = data['attempt_by_id'].get(k.attempt_id)
        if attempt is None:
            continue
        designer = data['assignment_by_id'][attempt.assignment_id].assigned_to
        for category, gate in ((k.qc_failure_category, 'Design QC'),
                               (k.head_failure_category, 'Design Head')):
            if not category:
                continue
            rows.append({
                'group':    error_category_group(category),
                'category': category,
                'label':    DESIGN_ERROR_CATEGORY_LABELS.get(category, category),
                'designer': designer,
                'gate':     gate,
                'source':   'Arka',
            })
    return rows


# ---------------------------------------------------------------------------
# 5. Group A — designer execution
# ---------------------------------------------------------------------------

def m_first_pass_rate(data):
    """Released sites released on attempt 1, per designer and team-wide.

    The denominator is RELEASED sites, not allocated ones: a site still in design has not
    yet had a chance to be first-pass, and counting it would drag every rate down by
    however much work happens to be in flight on the day the page is opened.
    """
    per = {}
    team_num = team_den = 0
    for s in data['sites']:
        if not s['released']:
            continue
        row = _bucket(per, s['designer'], {'first': 0, 'released': 0})
        row['released'] += 1
        team_den += 1
        if s['assignment'].current_attempt_number == 1:
            row['first'] += 1
            team_num += 1
    return {
        'team': rate(team_num, team_den),
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': rate(r['first'], r['released']),
        }),
    }


def m_rework_multiplier(data):
    """Attempts per released site, GROUP A CAUSES ONLY.

    THE NUMERATOR IS NOT "every attempt". classify_attempt_causes() says what opened each
    attempt, and everything that is not the designer's own error comes out:

        kept      the initial attempt, and every attempt a Group A failure opened
        kept      uncategorised attempts — pre-Part-9 QC failures, which had no other
                  meaning at the time. Counted, and reported separately so the mixture is
                  visible rather than implied (the reasoning is design_metrics'; it is
                  not re-litigated here).
        REMOVED   attempts opened by a Group B or Group C failure
        REMOVED   attempts opened by a PM change request

    That last exclusion is where this diverges from the tender dashboard's rework column,
    which keeps PM-change attempts in. Both are defensible for what they name; this one is
    the figure a hard rule of Part 10 requires, and the divergence is printed on screen so
    two different numbers under similar labels do not read as a bug.
    """
    per = {}
    for s in data['sites']:
        row = _bucket(per, s['designer'],
                      {'released': 0, 'designer_attempts': 0, 'uncategorised': 0,
                       'excluded': 0})
        if s['released']:
            row['released'] += 1
        causes = classify_attempt_causes(s['attempts'])
        for t in s['attempts']:
            cause = causes.get(t.attempt_number)
            if cause in (ERROR_GROUP_B, ERROR_GROUP_C, CAUSE_PM_CHANGE):
                row['excluded'] += 1
                continue
            row['designer_attempts'] += 1
            if cause == CAUSE_UNCATEGORISED:
                row['uncategorised'] += 1
    return {
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': ratio(r['designer_attempts'], r['released'], places=2),
            'uncategorised': r['uncategorised'],
            'excluded': r['excluded'],
        }),
    }


def m_arka_iterations(data):
    """Average Arka versions per attempt, per designer.

    CARRIED-FORWARD VERSIONS ARE EXCLUDED. Part 9.1 copies an Arka onto the next attempt
    when the failure did not name it, and those copies carry `carried_forward_from`.
    Counting them as iterations would charge a designer for versions nobody produced —
    the failure was in the CAD or the BOQ and the layout was never touched.

    Attempts with no Arka at all are out of the denominator: an attempt that never reached
    a submission has not had zero iterations, it has had none yet.
    """
    per = {}
    for s in data['sites']:
        row = _bucket(per, s['designer'], {'versions': 0, 'attempts': 0})
        for t in s['attempts']:
            own = [k for k in data['arkas_by_attempt'].get(t.pk, [])
                   if k.carried_forward_from_id is None]
            if not own:
                continue
            row['attempts'] += 1
            row['versions'] += len(own)
    return {
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': ratio(r['versions'], r['attempts'], places=2),
        }),
    }


def _gate_failure_rate(data, verdict_attr):
    per = {}
    team_num = team_den = 0
    for s in data['sites']:
        row = _bucket(per, s['designer'], {'failed': 0, 'ruled': 0})
        for t in s['attempts']:
            verdict = getattr(t, verdict_attr)
            # 'pending' means NOT JUDGED, never "judged and undecided" — see the
            # DesignAttempt docstring. An unjudged attempt is out of the denominator.
            if verdict == QC_PENDING:
                continue
            row['ruled'] += 1
            team_den += 1
            if verdict == QC_FAILED:
                row['failed'] += 1
                team_num += 1
    return {
        'team': rate(team_num, team_den),
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': rate(r['failed'], r['ruled']),
        }),
    }


def m_qc_failure_rate(data):
    """Failed at gate 1, over attempts Design QC actually ruled on."""
    return _gate_failure_rate(data, 'qc_verdict')


def m_head_failure_rate(data):
    """Failed at gate 2, over attempts that reached it.

    "REACHED THE GATE" IS READ FROM head_verdict, NOT head_started_at. head_started_at was
    added by Part 9 and is null on every attempt the Head ruled on before it existed —
    on this database it is set on 2 attempts against 12 recorded Head verdicts. Using it
    as the denominator would report a failure rate over a sixth of the real population.
    """
    return _gate_failure_rate(data, 'head_verdict')


def m_error_distribution(data):
    """Group A categories, team-wide and per designer. THE TRAINING SIGNAL.

    Group B and C rows are dropped here and counted by their own metrics. That is the
    hard rule, and it is enforced by filtering on error_category_group() rather than by
    listing which categories are designer errors.
    """
    rows = [r for r in _failure_rows(data) if r['group'] == ERROR_GROUP_A]
    team = {}
    per = {}
    for r in rows:
        team[r['category']] = team.get(r['category'], 0) + 1
        who = _bucket(per, r['designer'], {'counts': dict})
        who['counts'][r['category']] = who['counts'].get(r['category'], 0) + 1

    def _sorted(counts):
        # Ordered by count, then by label — this is a distribution of ERROR TYPES, not of
        # people, so ranking it ranks nothing about anybody.
        return [{'category': c, 'label': DESIGN_ERROR_CATEGORY_LABELS.get(c, c), 'count': n}
                for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        'total': len(rows),
        'team':  _sorted(team),
        'rows':  _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'counts': _sorted(r['counts']),
            'total': sum(r['counts'].values()),
        }),
        'by_source': {
            'package': sum(1 for r in rows if r['source'] == 'Package'),
            'arka':    sum(1 for r in rows if r['source'] == 'Arka'),
        },
    }


def m_capacity_throughput(data):
    """kW released per designer, from the approved Arka on each released site.

    Sites released with no approved Arka capacity are counted separately rather than as
    0 kW — a designer with three such sites otherwise reads as having released nothing.
    """
    per = {}
    for s in data['sites']:
        if not s['released']:
            continue
        row = _bucket(per, s['designer'], {'kw': Decimal('0'), 'sites': 0, 'no_capacity': 0})
        row['sites'] += 1
        if s['capacity_kw'] is not None:
            row['kw'] += s['capacity_kw']
        else:
            row['no_capacity'] += 1
    return {
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'kw': r['kw'], 'sites': r['sites'], 'no_capacity': r['no_capacity'],
        }),
        'total_kw': sum((r['kw'] for r in per.values()), Decimal('0')),
    }


# ---------------------------------------------------------------------------
# 6. Group B — input quality
# ---------------------------------------------------------------------------

def m_hold_rate(data):
    """Sites EVER placed on Design Hold, over sites allocated. Team-wide only.

    "EVER", not "currently". `survey_returned_at` is a permanent marker: the unblock path
    deliberately leaves the triple in place when it clears a hold, so a non-null stamp
    means the site was held at some point even though its status has long since moved on.
    Filtering on status instead would report only the sites held at this instant, which on
    a healthy tender is zero and would make the metric look like good news.

    NO PER-DESIGNER SPLIT, and that is the design. This measures the survey the site
    arrived with. Attaching a designer's name to it is exactly the misreading the whole
    Group A/B/C split exists to prevent.
    """
    held = sum(1 for s in data['sites'] if s['ever_held'])
    allocated = sum(1 for s in data['sites'] if s['allocated'])
    return {
        'figure': rate(held, allocated),
        'held': held,
        'allocated': allocated,
        'currently_held': sum(1 for s in data['sites']
                              if s['assignment'].status == DESIGN_SURVEY_RETURNED),
    }


def _group_failures(data, group):
    rows = [r for r in _failure_rows(data) if r['group'] == group]
    counts = {}
    for r in rows:
        counts[r['category']] = counts.get(r['category'], 0) + 1
    return {
        'total': len(rows),
        'counts': [{'category': c, 'label': DESIGN_ERROR_CATEGORY_LABELS.get(c, c),
                    'count': n}
                   for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def m_group_b_failures(data):
    """Input-problem failures. Reported here and folded into NO designer figure."""
    return _group_failures(data, ERROR_GROUP_B)


def m_hold_duration(data):
    """Average days on Design Hold, reconstructed from paired activity-log events.

    WHY NOT FROM THE ASSIGNMENT ROW: DesignAssignment carries one survey_returned_at,
    overwritten on each hold, so the model alone answers only "the most recent one, if it
    was cleared". The log carries both ends of every hold — `design_blocked` on entry and
    `design_survey_unblocked` on exit — so pairing them per project recovers the history
    without a schema change.

    THREE HONEST LIMITS, all surfaced on screen: log_activity() swallows its own
    exceptions so a missing row is silently absent; a hold with no matching exit is OPEN
    and is counted as open rather than measured against today, which would grow every time
    the page is refreshed; and a second `design_blocked` with no exit between the two is
    treated as a continuation of the same hold rather than a new one.
    """
    open_hold = {}
    durations = []
    still_open = 0
    for project_id, code, ts in data['hold_events']:
        if code == 'design_blocked':
            open_hold.setdefault(project_id, ts)
        elif code == 'design_survey_unblocked':
            started = open_hold.pop(project_id, None)
            if started is not None:
                days = _working_days(_localdate(started), _localdate(ts))
                if days is not None:
                    durations.append(days)
    still_open = len(open_hold)
    return {
        'spread': _spread(durations),
        'completed': len(durations),
        'still_open': still_open,
        'events': len(data['hold_events']),
    }


# ---------------------------------------------------------------------------
# 7. Group C — brief stability
# ---------------------------------------------------------------------------

def m_change_request_rate(data):
    """Accepted change requests over released sites, PER REQUESTING PM.

    THE PM IS THE UNIT, not the designer, and that is the whole point of the metric. The
    denominator for one PM is the released sites THEY are the assigned PM of — dividing
    every PM's accepted requests by the tender's total released count would make a PM
    holding two sites look identical to one holding forty.
    """
    per = {}
    for s in data['sites']:
        row = _bucket(per, s['pm'], {'released': 0, 'accepted': 0})
        if s['released']:
            row['released'] += 1
    for cr in data['change_requests']:
        if cr.verdict != CHANGE_REQUEST_ACCEPTED:
            continue
        row = _bucket(per, cr.requested_by, {'released': 0, 'accepted': 0})
        row['accepted'] += 1

    team_accepted = sum(1 for cr in data['change_requests']
                        if cr.verdict == CHANGE_REQUEST_ACCEPTED)
    team_released = sum(1 for s in data['sites'] if s['released'])
    return {
        'team': ratio(team_accepted, team_released, places=2),
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': ratio(r['accepted'], r['released'], places=2),
            'accepted': r['accepted'], 'released': r['released'],
        }),
    }


def m_cr_rejection_rate(data):
    """Rejected over ALL requests raised — does the Head's triage actually refuse anything.

    The denominator is every request, pending included, exactly as the catalogue words it.
    The pending count is reported beside the figure so a low rate caused by an untriaged
    backlog is not mistaken for a low rate caused by a rubber stamp.
    """
    crs = data['change_requests']
    rejected = sum(1 for cr in crs if cr.verdict == CHANGE_REQUEST_REJECTED)
    accepted = sum(1 for cr in crs if cr.verdict == CHANGE_REQUEST_ACCEPTED)
    pending  = sum(1 for cr in crs if cr.verdict == CHANGE_REQUEST_PENDING)
    return {
        'figure': rate(rejected, len(crs)),
        'rejected': rejected, 'accepted': accepted, 'pending': pending,
        'total': len(crs),
    }


def m_group_c_failures(data):
    """Brief-change failures. Reported here and folded into NO designer figure."""
    return _group_failures(data, ERROR_GROUP_C)


def m_cr_by_stage(data):
    """How late the brief moved — derived, and labelled as derived.

    THE STAGE AT REQUEST TIME IS NOT STORED. What is stored is when the request arrived and
    when that attempt passed each gate, so the bucket is inferred by comparing them:

        In design         the request predates QC starting on that attempt
        With Design QC    QC had started, the Head had not yet received it
        With the Head     the package had reached the Head gate

    That inference is sound for the ordering of stamps it reads and no more. The attempt
    number beside it needs no inference at all — a request on attempt 3 is late by
    definition — so both are shown and the exact one is not hidden behind the derived one.
    """
    buckets = {'in_design': 0, 'in_qc': 0, 'with_head': 0}
    by_attempt = {}
    for cr in data['change_requests']:
        attempt = data['attempt_by_id'].get(cr.attempt_id)
        if attempt is None:
            continue
        head_at = attempt.head_started_at or attempt.qc_reviewed_at
        if head_at is not None and cr.requested_at >= head_at:
            buckets['with_head'] += 1
        elif attempt.qc_started_at is not None and cr.requested_at >= attempt.qc_started_at:
            buckets['in_qc'] += 1
        else:
            buckets['in_design'] += 1
        n = attempt.attempt_number
        by_attempt[n] = by_attempt.get(n, 0) + 1
    return {
        'total': len(data['change_requests']),
        'stages': [
            {'label': 'Raised while in design',      'count': buckets['in_design']},
            {'label': 'Raised while with Design QC', 'count': buckets['in_qc']},
            {'label': 'Raised while with the Head',  'count': buckets['with_head']},
        ],
        'by_attempt': [{'attempt': n, 'count': c}
                       for n, c in sorted(by_attempt.items())],
    }


# ---------------------------------------------------------------------------
# 8. Group D — process and reviewer consistency
# ---------------------------------------------------------------------------

def m_overturn_rate(data):
    """Head verdict differing from Design QC's, per QC REVIEWER and overall.

    `head_overturned_qc` is a stored boolean on both DesignAttempt and ArkaSubmission,
    written at verdict time precisely so this is countable rather than reconstructed. Both
    artifact kinds are counted, because a reviewer whose package verdicts always stand but
    whose Arka approvals are routinely reversed is exactly the case the metric is for.

    THE DENOMINATOR IS ARTIFACTS THAT REACHED THE HEAD GATE, read from the Head's verdict
    being non-pending. A QC failure ends the attempt and the Head never sees it, so those
    rows are correctly absent from both halves.

    READ THE RESULT THE RIGHT WAY ROUND. A rate near zero across two tenders does not mean
    the reviewers agree; it means the second gate is not changing outcomes and is not
    earning its cost. That reading is printed on the panel, because a zero here looks like
    good news and is not.
    """
    per = {}
    team_num = team_den = 0
    for t in data['attempts']:
        if t.head_verdict == QC_PENDING:
            continue
        row = _bucket(per, t.qc_reviewed_by,
                      {'overturned': 0, 'reviewed': 0,
                       'pkg_overturned': 0, 'pkg_reviewed': 0,
                       'arka_overturned': 0, 'arka_reviewed': 0})
        row['reviewed'] += 1
        row['pkg_reviewed'] += 1
        team_den += 1
        if t.head_overturned_qc:
            row['overturned'] += 1
            row['pkg_overturned'] += 1
            team_num += 1
    for k in data['arkas']:
        if k.head_verdict == ARKA_PENDING:
            continue
        row = _bucket(per, k.reviewed_by,
                      {'overturned': 0, 'reviewed': 0,
                       'pkg_overturned': 0, 'pkg_reviewed': 0,
                       'arka_overturned': 0, 'arka_reviewed': 0})
        row['reviewed'] += 1
        row['arka_reviewed'] += 1
        team_den += 1
        if k.head_overturned_qc:
            row['overturned'] += 1
            row['arka_overturned'] += 1
            team_num += 1
    return {
        'team': rate(team_num, team_den),
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': rate(r['overturned'], r['reviewed']),
            'pkg': f"{r['pkg_overturned']}/{r['pkg_reviewed']}",
            'arka': f"{r['arka_overturned']}/{r['arka_reviewed']}",
        }),
    }


def m_on_time_delivery(data):
    """Released on or before the APPROVED due date, per designer.

    THE APPROVED DATE, NOT THE CURRENT ONE. design_metrics.effective_commitment() returns
    the most recently APPROVED commitment rather than the `is_current` row, and that
    distinction is the metric: a designer who requests an extension takes over `is_current`
    with an unapproved row, and reading it here would let anybody clear their own late
    delivery by asking for more time after the fact. A pending extension moves nothing.

    Sites released with no approved date ever are excluded from both halves and counted
    separately — there is no date to have been on time against.
    """
    per = {}
    team_num = team_den = 0
    no_due = 0
    for s in data['sites']:
        if not s['released'] or s['assignment'].released_at is None:
            continue
        commitment = effective_commitment(s['commitments'])
        if commitment is None:
            no_due += 1
            continue
        row = _bucket(per, s['designer'], {'ontime': 0, 'released': 0})
        row['released'] += 1
        team_den += 1
        if _localdate(s['assignment'].released_at) <= commitment.proposed_date:
            row['ontime'] += 1
            team_num += 1
    return {
        'team': rate(team_num, team_den),
        'no_due_date': no_due,
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'figure': rate(r['ontime'], r['released']),
        }),
    }


def m_extension_rate(data):
    """Assignments carrying more than one due-date commitment, over those with any.

    Revisions are derived by COUNTING ROWS, exactly as Part 2 established: an approved date
    is never edited in place, a change writes a new row, so `count - 1` is the revision
    count and there is no stored counter that can drift. Assignments with no commitment at
    all are out of the denominator — a site that never got a date has not declined to
    extend one.
    """
    with_any = extended = 0
    for s in data['sites']:
        if not s['commitments']:
            continue
        with_any += 1
        if len(s['commitments']) > 1:
            extended += 1
    return {'figure': rate(extended, with_any),
            'extended': extended, 'with_any': with_any}


def m_cycle_time(data):
    """Working days from allocation to release — median and the range around it.

    Working days, via utils.is_working_day, so a site released on a Monday after a Friday
    allocation is one day and not three. Public holidays are not modelled anywhere in the
    product; that gap is already recorded in DESIGN_MODULE_DEFERRED.md and is not widened
    here.
    """
    per = {}
    overall = []
    for s in data['sites']:
        if not s['released']:
            continue
        days = _working_days(_localdate(s['assignment'].assigned_at),
                             _localdate(s['assignment'].released_at))
        if days is None:
            continue
        overall.append(days)
        row = _bucket(per, s['designer'], {'days': list})
        row['days'].append(days)
    return {
        'team': _spread(overall),
        'rows': _person_rows(per, lambda r: {
            'label': r['label'], 'profile': r['profile'],
            'spread': _spread(r['days']),
        }),
    }


#: The intervals the workflow ACTUALLY timestamps. Not statuses — see the module docstring.
#: Each is (label, what it measures), and the arithmetic below names its two stamps
#: explicitly so a reader can check the claim against the model.
DWELL_INTERVALS = [
    ('Allocation → first Arka submitted',   'Designer working before anything is submitted'),
    ('Arka submitted → Design QC verdict',  'Waiting on gate 1 for the layout'),
    ('Arka QC verdict → Head verdict',      'Waiting on gate 2 for the layout'),
    ('QC started → Design QC verdict',      'Gate 1 reviewing the package'),
    ('Reached Head gate → Head verdict',    'Gate 2 reviewing the package'),
    ('Head verdict → released',             'Administrative tail after the last approval'),
]


def m_stage_dwell(data):
    """Median working days per INTERVAL. Explicitly not per status.

    THE SIX INTERVALS ARE THE COMPLETE SET the existing timestamps can support, and the
    gap between them is where the unmeasured time hides: nothing stamps entry into
    `in_design`, `artifacts_uploaded`, `in_qc`, `arka_rejected` or either waiting room, so
    the time between an Arka being approved and the package being complete is not here and
    cannot be. The panel says so rather than letting the six sum look like a whole.
    """
    buckets = {label: [] for label, _ in DWELL_INTERVALS}

    def _add(label, start, end):
        days = _working_days(_localdate(start), _localdate(end))
        if days is not None:
            buckets[label].append(days)

    for s in data['sites']:
        for t in s['attempts']:
            own_arkas = sorted(data['arkas_by_attempt'].get(t.pk, []),
                               key=lambda k: k.version)
            fresh = [k for k in own_arkas if k.carried_forward_from_id is None]
            if fresh:
                _add('Allocation → first Arka submitted',
                     s['assignment'].assigned_at, fresh[0].submitted_at)
            for k in fresh:
                _add('Arka submitted → Design QC verdict', k.submitted_at, k.reviewed_at)
                _add('Arka QC verdict → Head verdict', k.reviewed_at, k.head_reviewed_at)
            _add('QC started → Design QC verdict', t.qc_started_at, t.qc_reviewed_at)
            # head_started_at is null on pre-Part-9 rows; qc_reviewed_at is when the
            # package entered the Head's queue on those, and is the same instant on new
            # ones. Same fallback reasoning as m_head_failure_rate().
            _add('Reached Head gate → Head verdict',
                 t.head_started_at or t.qc_reviewed_at, t.head_reviewed_at)
        if s['final_attempt'] is not None:
            _add('Head verdict → released',
                 s['final_attempt'].head_reviewed_at, s['assignment'].released_at)

    return {
        'rows': [{'label': label, 'blurb': blurb, 'spread': _spread(buckets[label])}
                 for label, blurb in DWELL_INTERVALS],
    }


def m_queue_latency(data):
    """How long an item waited for each gate's verdict, split by artifact.

    Measured from when the item ENTERED that queue to when the verdict landed, so it is
    the reviewer's response time and not the designer's working time. The Head-package
    figure falls back to qc_reviewed_at where head_started_at is null, for the same reason
    as everywhere else in this module.
    """
    arka_qc, arka_head, pkg_qc, pkg_head = [], [], [], []
    for k in data['arkas']:
        if k.carried_forward_from_id is not None:
            continue
        d = _working_days(_localdate(k.submitted_at), _localdate(k.reviewed_at))
        if d is not None:
            arka_qc.append(d)
        d = _working_days(_localdate(k.reviewed_at), _localdate(k.head_reviewed_at))
        if d is not None:
            arka_head.append(d)
    for t in data['attempts']:
        d = _working_days(_localdate(t.qc_started_at), _localdate(t.qc_reviewed_at))
        if d is not None:
            pkg_qc.append(d)
        d = _working_days(_localdate(t.head_started_at or t.qc_reviewed_at),
                          _localdate(t.head_reviewed_at))
        if d is not None:
            pkg_head.append(d)
    return {
        'rows': [
            {'label': 'Arka — waiting for a Design QC verdict', 'spread': _spread(arka_qc)},
            {'label': 'Arka — waiting for a Head verdict',      'spread': _spread(arka_head)},
            {'label': 'Package — waiting for a Design QC verdict', 'spread': _spread(pkg_qc)},
            {'label': 'Package — waiting for a Head verdict',      'spread': _spread(pkg_head)},
        ],
    }


# ---------------------------------------------------------------------------
# 9. Dispatch
# ---------------------------------------------------------------------------
# ONE ENTRY PER CATALOGUE KEY, and compute() runs only the metrics that are selected.
# A metric switched off costs nothing beyond the batched reads, which are shared.

_COMPUTE = {
    'first_pass_rate':     m_first_pass_rate,
    'rework_multiplier':   m_rework_multiplier,
    'arka_iterations':     m_arka_iterations,
    'qc_failure_rate':     m_qc_failure_rate,
    'head_failure_rate':   m_head_failure_rate,
    'error_distribution':  m_error_distribution,
    'capacity_throughput': m_capacity_throughput,
    'hold_rate':           m_hold_rate,
    'group_b_failures':    m_group_b_failures,
    'hold_duration':       m_hold_duration,
    'change_request_rate': m_change_request_rate,
    'cr_rejection_rate':   m_cr_rejection_rate,
    'group_c_failures':    m_group_c_failures,
    'cr_by_stage':         m_cr_by_stage,
    'overturn_rate':       m_overturn_rate,
    'on_time_delivery':    m_on_time_delivery,
    'extension_rate':      m_extension_rate,
    'cycle_time':          m_cycle_time,
    'stage_dwell':         m_stage_dwell,
    'queue_latency':       m_queue_latency,
}


def compute(programs, selected):
    """Every selected metric, in catalogue order, over the given tenders.

    Returns a list of panels rather than a dict, so the template renders in the
    catalogue's order — grouped A, B, C, D — without holding its own copy of that order.
    """
    data = analytics_dataset(programs, need_hold_events='hold_duration' in selected)
    panels = []
    for metric in METRIC_CATALOGUE:
        if metric.key not in selected:
            continue
        panels.append({
            'metric': metric,
            'group':  metric.group,
            'data':   _COMPUTE[metric.key](data),
        })
    return {
        'panels': panels,
        'site_count': len(data['sites']),
        'released_count': sum(1 for s in data['sites'] if s['released']),
        'attempt_count': len(data['attempts']),
    }
