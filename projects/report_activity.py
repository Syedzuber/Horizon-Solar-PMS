"""
RPT-B1 — who was active, and where, over a reporting Period. Pure reads, no view.

    report = activity_report(period)              # the period AND period.previous
    report.current.active_users.rows              # per user: actions, sites, last action
    report.current.active_sites.in_execution      # per site, split execution / design
    report.current.bulk_actions                   # each bulk batch, counted once
    row.kinds                                     # per user: (kind label, count), most first
    active_users(period).current / .previous      # one figure, both periods
    accounts_by_role()                            # active accounts per role, one query

Every public function takes a report_periods.Period and issues a FIXED number of queries
(ten) whatever the period's length or the number of sites: the rows of both periods are
fetched together and split in Python. A count is never re-derived by a second query.

DEFINITIONS (decisions D4-D11 and Zuber's pre-flight rulings 1-7, 28 Nov 2026)

    action          one of:
                      - an ActivityLog row passing site_dashboard.human_activity_q() (logins,
                        logouts, *_opened reads and the scheduler's codes are out);
                      - a contractor attendance entry, not voided, on its `date` and timed by
                        `recorded_at` (attendance writes no ActivityLog);
                      - a StatusTransition row with an actor ONLY when it is UNPAIRED: no
                        ActivityLog row by the same actor on the same site (NULL matched
                        with NULL) within PAIR_WINDOW. Nearly every ledger row has its log
                        twin written in the same request (2979 of 2980 on production), so
                        counting both would add ~56% phantom actions (RPT-B1 pre-flight 3).
                    A login alone is not an action.
    bulk action     a run by the same actor with the same code, each row within
                    activity_codes.BULK_BATCH_GAP_SECONDS of the previous, touching at least
                    BULK_MIN_SITES distinct sites. It counts as ONE action for its actor, on
                    the period its first row falls in (edge case 3: a batch crossing
                    midnight IST belongs to the day it started), and it never makes a site
                    active. Unpaired ledger rows are batched by the same rule, their code
                    being "subject_type:to_status". Batches by the same actor that start
                    within BULK_MERGE_SECONDS of each other over the same set of sites are
                    ONE bulk action showing every code (B1 answer 1): design_bulk_allocate
                    writes design_allocated and design_qc_assigned per site in one POST.
    active user     an active account AND active profile with at least one action. Shown
                    with their CURRENT role (edge case 7). Deactivated users are left out of
                    every list and total (D11); their rows still make a site active.
    signed in, no action
                    an active user with a 'user_login' row in the period and no action. Not
                    "opened the app": sessions last two weeks, so a returning user writes no
                    login row (RPT-A T2).
    active site     a live site (permissions.dashboard_live_sites(): not deleted, not test,
                    not Cancelled) with a non-bulk ActivityLog human activity row in the
                    period — the rule site_breakdown()'s "Active today" applies, over a
                    longer window, through the same human_activity_between(). Attendance
                    never makes a site active (5 Nov rule). Rows with no actor count for the
                    site, as they do there. Split by the site's status NOW (edge case 5): a
                    Draft is "in design", anything else "in execution"; neither includes
                    the other. A site whose only rows are bulk is listed apart and is not
                    active.
    open tasks, no activity
                    an active user holding at least one open task (any task type, not Done,
                    not a mirror, not N/A) on a live site, with no action in the period.
                    Open and overdue counts are AS OF NOW, never rebuilt for a past period;
                    overdue is the PM card's rule, site_dashboard.card_overdue_q().
    site label      site_code, else project_id (ruling 4); the site's name travels
                    separately and never in the label.

WHICH SITES COUNT FOR WHAT (D11 and B1 answer 2)
    test or deleted site   dropped everywhere: no action, no site, no user credit
    Cancelled site         its actions CREDIT THE USER (their action count and site list)
                           but it is never an active site, nor a bulk-only one
    every other site       counts for the user and, under the rule above, as active
Rows with no site are kept: a Design BOQ group or a material approval is a real action
with nowhere to put it (edge case 2), counted under `actions_without_site`.

ACTION KINDS (RPT-B2a, Zuber's Q2/Q3 answers, 28 Nov 2026)
    Every action a user is credited with is also counted under a KIND, in the same pass
    that counts it, so a user's kinds always add up to their actions: there is one
    counting path, not a second one that could drift. The kind is a readable label:
      - an ActivityLog row with a code: action_kind_label(code)
      - an ActivityLog row with NO code (the ~80 legacy call sites): its entity type,
        ENTITY_KIND_LABELS, read from the same fetch (no extra query)
      - an unpaired ledger row: "Task → Done"
      - attendance: ATTENDANCE_KIND
      - a bulk action: "Bulk: " and the labels of every code it merged
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.db.models import Count, Exists, F, Max, OuterRef, Q, Value
from django.db.models.functions import Coalesce, NullIf
from django.utils import timezone

from .activity_codes import BULK_BATCH_GAP_SECONDS, BULK_MERGE_SECONDS, BULK_MIN_SITES
from .models import (ActivityLog, ContractorAttendance, Project, StatusTransition, Task,
                     UserProfile)
from .permissions import dashboard_live_sites
from .site_breakdown import DRAFT_STATUS
from .site_dashboard import card_overdue_q, human_activity_between, human_activity_q
from .task_health import OPEN_STATUSES
from .utils import applicable_tasks_q, human_owned_tasks_q


#: The login row signals.log_user_login() writes. Already in NON_ACTIVITY_CODES, so it is
#: never an action; read here only for "signed in, no action".
LOGIN_ACTION_CODE = 'user_login'

#: How close a StatusTransition must be to an ActivityLog row (same actor, same site) to be
#: the same action. The two are written in one request, milliseconds apart; 5 s is the
#: window the RPT-B1 pre-flight measured against (2979 of 2980 rows paired).
PAIR_WINDOW = timedelta(seconds=5)

#: Rows are fetched this far beyond the periods so a bulk batch that runs over a period's
#: edge is still seen whole: its size decides whether it is bulk at all. A real bulk POST
#: lasts about a second (30 sites in 1 s on 25 Nov); ten minutes of 2-second gaps would
#: be a 300-row batch, far longer than any seen.
BATCH_LOOKAROUND = timedelta(minutes=10)

#: The Project status dashboard_live_sites() leaves out. A Cancelled site's actions still
#: credit the person who did them (B1 answer 2); the site is never an active site.
CANCELLED_STATUS = 'Cancelled'

STAGE_EXECUTION = 'execution'
STAGE_DESIGN = 'design'
STAGE_CANCELLED = 'cancelled'   # credits users only; never counted as an active site

SOURCE_LOG = 'log'        # a bulk batch of ActivityLog rows
SOURCE_LEDGER = 'ledger'  # a bulk batch of unpaired StatusTransition rows


# ---------------------------------------------------------------------------
# Site label (D10, ruling 4)
# ---------------------------------------------------------------------------

def site_label(project):
    """The site's short name in every report: site_code, else project_id. Residential sites
    have no site_code, so they read as their HRP-RES-… id. Never the customer's name."""
    return project.site_code or project.project_id


def site_label_expr(prefix=''):
    """site_label() as a queryset expression, for .annotate(label=site_label_expr()).
    NullIf turns a blank site_code into NULL so Coalesce falls through to project_id, the
    same as `or` does above; the two are pinned equal by tests_report_foundations."""
    return Coalesce(NullIf(F(f'{prefix}site_code'), Value('')), F(f'{prefix}project_id'))


def site_name(project):
    """The site's longer name, shown beside the label: site_name, else customer_name."""
    return project.site_name or project.customer_name or ''


def _person_name(profile):
    user = profile.user
    return user.get_full_name() or user.username


# ---------------------------------------------------------------------------
# Action kinds (RPT-B2a, Q3): the readable name of what an action was
# ---------------------------------------------------------------------------
# There is no code -> label map anywhere in the app (pre-flight 3), and ActivityLog's
# `action` text is free-form (it carries task names and DC numbers), so grouping by it
# would scatter one kind of work over dozens of rows. The label is built from the code
# instead, by the rule Zuber confirmed: drop a leading "design_", underscores to spaces,
# sentence case, short forms in capitals. design_head_qc_passed -> "Head QC passed".
# Since RPT-C1 one map does exist, CODE_KIND_LABELS, for the codes given to the formerly
# blank call sites only; it is read first and the rule covers every other code.

#: Words kept in capitals when a code is turned into words.
SHORT_FORMS = frozenset({'qc', 'qa', 'boq', 'pm', 'se', 'accb', 'acdb', 'dcdb', 'grn', 'dc',
                         'po', 'pi', 'cod'})

#: The prefix dropped from a code: most of the app's codes are design workflow codes, and
#: on a report of who did what, "Design" in front of each of them says nothing.
DROPPED_CODE_PREFIX = 'design_'

ATTENDANCE_KIND = 'Attendance recorded'
BULK_KIND_PREFIX = 'Bulk: '

#: The kind of an ActivityLog row with NO action_code, by its entity_type (Q3 (ii)). The
#: legacy call sites never got a code (migration 0043 left them blank on purpose); their
#: entity type is the stable part of what they wrote. Built from every entity type a
#: blank-code human activity row carries on the 27 Nov production restore.
ENTITY_KIND_LABELS = {
    'Task': 'Task update',                    # nearly all "Updated due date for task: …"
    'Project': 'Project update',              # activated, coordinators, fields edited
    'User': 'User account',                   # profile edits (and pre-28 Jul login rows)
    'File': 'File upload',
    'ChecklistItemCompletion': 'Checklist answered',
    'DeliveryChallan': 'Delivery challan',    # DC created, GRN confirmed
    'Notification': 'Notification setting',   # "Notification pref updated for …"
    'NotificationEventSetting': 'Notification setting',
    'Milestone': 'Milestone update',
    'Issue': 'Issue update',
    'BOQItemMaster': 'BOQ catalogue edit',
    'Comment': 'Comment',
    'System': 'System setting',               # master switches, feature gates
    'PaymentRequest': 'Payment request',
}
#: A blank-code row with no entity type either.
OTHER_KIND = 'Other'

#: Labels for the codes RPT-C1 gave the formerly blank call sites
#: (activity_codes.FORMERLY_BLANK_CODES), read BEFORE the naming rule (Zuber's C4 ruling,
#: 28 Nov 2026). Spelt out because the rule would say "Project activated" where the
#: Reports section says "site" everywhere, and "Task commented" for a comment. The keys
#: equal FORMERLY_BLANK_CODES exactly; tests_activity_codes_c1 fails if the two drift.
CODE_KIND_LABELS = {
    'project_activated': 'Site activated',
    'project_fields_edited': 'Site fields edited',
    'project_coordinators_updated': 'Coordinators updated',
    'cascade_scheduling_enabled': 'Cascade scheduling enabled',
    'project_created_from_zoho': 'Site created from Zoho',
    'boq_acknowledged': 'BOQ acknowledged',
    'boq_submitted': 'BOQ submitted',
    'boq_revision_requested': 'BOQ revision requested',
    'project_file_uploaded': 'Site file uploaded',
    'task_file_uploaded': 'Task file uploaded',
    'file_deleted': 'File deleted',
    'checklist_item_answered': 'Checklist answered',
    'task_commented': 'Comment on task',
    'task_comment_replied': 'Reply on task',
    'issue_commented': 'Comment on issue',
    'issue_comment_replied': 'Reply on issue',
    'task_comment_deleted': 'Task comment deleted',
    'issue_comment_deleted': 'Issue comment deleted',
    'issue_in_progress': 'Issue in progress',
    'issue_assigned': 'Issue assigned',
    'issue_unassigned': 'Issue unassigned',
    'dc_created': 'DC created',
    'grn_confirmed': 'GRN confirmed',
    'grn_overridden': 'GRN overridden',
    'system_setting_changed': 'System setting changed',
    'user_deactivated': 'User deactivated',
    'user_reactivated': 'User reactivated',
    'user_role_changed': 'User role changed',
    'user_profile_updated': 'User profile updated',
    'user_password_reset': 'User password reset',
    'user_created': 'User created',
    'notification_pref_changed': 'Notification preference changed',
    'notify_event_switched': 'Notification switched',
    'notify_template_activated': 'Notification template activated',
    'notify_template_draft_discarded': 'Notification draft discarded',
    'notify_template_restored': 'Notification version restored',
    'checklist_created': 'Checklist created',
    'checklist_published': 'Checklist published',
    'checklist_archived': 'Checklist archived',
    'checklist_renamed': 'Checklist renamed',
    'checklist_deleted': 'Checklist deleted',
    'checklist_item_added': 'Checklist item added',
    'checklist_item_edited': 'Checklist item edited',
    'checklist_item_deleted': 'Checklist item deleted',
    'checklist_item_moved': 'Checklist item moved',
    'checklist_linked': 'Checklist linked',
    'checklist_unlinked': 'Checklist unlinked',
    'stock_location_created': 'Warehouse created',
    'stock_location_updated': 'Warehouse updated',
    'stock_location_activated': 'Warehouse activated',
    'stock_location_deactivated': 'Warehouse deactivated',
    'boq_catalogue_item_created': 'BOQ catalogue item created',
    'boq_catalogue_item_updated': 'BOQ catalogue item updated',
    'boq_catalogue_item_activated': 'BOQ catalogue item activated',
    'boq_catalogue_item_deactivated': 'BOQ catalogue item deactivated',
    'boq_catalogue_unit_fixed': 'Catalogue unit fixed',
    'due_date_changed': 'Due date changed',
    'milestone_updated': 'Milestone updated',
}

#: Ledger subjects (StatusTransition.subject_type) as they read on a report.
LEDGER_SUBJECT_LABELS = {
    'design_assignment': 'Design',
    'delivery_challan': 'DC',
    'payment_milestone': 'Milestone',
    'payment_request': 'Payment request',
    'payment_receipt': 'Receipt',
    'approval_request': 'Approval',
}


def _words(text):
    """`text` split on underscores and spaces, short forms in capitals, the rest as stored."""
    return [w.upper() if w.lower() in SHORT_FORMS else w
            for w in text.replace('_', ' ').split()]


def _sentence(words):
    """The words with the first one capitalised. A short form is already in capitals."""
    if not words:
        return ''
    first = words[0]
    return ' '.join([first[:1].upper() + first[1:]] + words[1:])


def ledger_kind_label(code):
    """'task:Done' -> 'Task → Done'; 'design_assignment:awaiting_head_qc' ->
    'Design → awaiting head QC'. The status keeps its stored case: task statuses are
    already words ('In Progress'), design ones are codes."""
    subject, _, status = code.partition(':')
    subject_text = LEDGER_SUBJECT_LABELS.get(subject) or _sentence(_words(subject))
    return f'{subject_text} → {" ".join(_words(status))}'


def entity_kind_label(entity_type):
    """The kind of a blank-code row: ENTITY_KIND_LABELS, else the entity type in words
    ('PurchaseOrder' -> 'Purchase order'), else OTHER_KIND."""
    if entity_type in ENTITY_KIND_LABELS:
        return ENTITY_KIND_LABELS[entity_type]
    if not entity_type:
        return OTHER_KIND
    # An entity type no blank-code row carried on 27 Nov: split its CamelCase so a new
    # one reads sensibly without a code change here.
    return _sentence(re.sub(r'(?<!^)(?=[A-Z])', ' ', entity_type).lower().split())


def action_kind_label(code, entity_type=''):
    """The readable kind of one action. `code` is an ActivityLog action_code, a ledger
    "subject_type:to_status" code, or blank (then `entity_type` decides)."""
    if not code:
        return entity_kind_label(entity_type)
    if code in CODE_KIND_LABELS:
        return CODE_KIND_LABELS[code]
    if ':' in code:
        return ledger_kind_label(code)
    if code.startswith(DROPPED_CODE_PREFIX) and len(code) > len(DROPPED_CODE_PREFIX):
        code = code[len(DROPPED_CODE_PREFIX):]
    return _sentence(_words(code))


def bulk_kind_label(codes):
    """'Bulk: Allocated + QC assigned' for a merged batch of design_allocated and
    design_qc_assigned. Codes in sorted order, as BulkAction.action_code joins them."""
    return BULK_KIND_PREFIX + ' + '.join(action_kind_label(c) for c in sorted(codes))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteInfo:
    id: int
    label: str                 # site_label()
    name: str                  # site_name()
    project_type: str          # stored value: 'OPEX' is shown as RESCO by the page
    program_id: Optional[int]
    program_label: Optional[str]
    stage: str                 # STAGE_EXECUTION / STAGE_DESIGN / STAGE_CANCELLED, from the status NOW


@dataclass(frozen=True)
class BulkAction:
    actor_id: int
    actor_name: str
    action_code: str           # the ActivityLog code, or "subject_type:to_status"
    source: str                # SOURCE_LOG or SOURCE_LEDGER
    started_at: datetime
    ended_at: datetime
    site_ids: tuple

    @property
    def site_count(self):
        return len(self.site_ids)


@dataclass(frozen=True)
class UserActivity:
    profile_id: int
    name: str
    role: str
    actions: int               # bulk batch = 1
    site_ids: tuple            # sites of their non-bulk actions and attendance, by label
    bulk_only_site_count: int  # sites reached only through their bulk batches
    actions_without_site: int
    last_action_at: datetime
    # ((kind label, count), ...), most first then by label; the counts add up to `actions`.
    # Defaulted, and last, so the field order B1 shipped is unchanged.
    kinds: tuple = ()


@dataclass(frozen=True)
class RoleTotal:
    role: str
    users: int
    actions: int


@dataclass(frozen=True)
class ActiveUsers:
    rows: tuple                # UserActivity, most recently active first
    by_role: tuple             # RoleTotal, by role name
    users: int
    actions: int
    actions_without_site: int


@dataclass(frozen=True)
class SignedInNoAction:
    profile_id: int
    name: str
    role: str
    sign_ins: int
    last_signed_in_at: datetime


@dataclass(frozen=True)
class OpenTasksNoActivity:
    profile_id: int
    name: str
    role: str
    open_tasks: int            # as of now
    overdue_tasks: int         # as of now, card_overdue_q
    last_action_at: Optional[datetime]   # their latest action at any time, None if never


@dataclass(frozen=True)
class SiteActivity:
    site: SiteInfo
    actions: int
    user_names: tuple          # active users with a non-bulk row here, by name
    last_activity_at: datetime


@dataclass(frozen=True)
class ActiveSites:
    rows: tuple                # SiteActivity, most actions first
    in_execution: int
    in_design: int
    bulk_only_site_ids: tuple  # touched only by bulk batches: NOT active, shown apart

    @property
    def total(self):
        return self.in_execution + self.in_design

    @property
    def bulk_only(self):
        return len(self.bulk_only_site_ids)


@dataclass(frozen=True)
class PeriodActivity:
    period: object             # report_periods.Period
    active_users: ActiveUsers
    signed_in_no_action: tuple
    open_tasks_no_activity: tuple
    active_sites: ActiveSites
    bulk_actions: tuple
    sites: dict                # {site id: SiteInfo} for every live site any row touched


@dataclass(frozen=True)
class ActivityReport:
    current: PeriodActivity
    previous: Optional[PeriodActivity]


@dataclass(frozen=True)
class Compared:
    """One figure for a period and its previous period (None when the period has none)."""
    current: object
    previous: object


# ---------------------------------------------------------------------------
# The fetch: ten queries for both periods
# ---------------------------------------------------------------------------

def _credited_sites():
    """The sites whose actions credit a user: dashboard_live_sites() (not deleted, not test,
    not Cancelled) plus the Cancelled ones (B1 answer 2). Built as that union rather than
    restating D9's filters, so a change to the live set reaches this too."""
    return dashboard_live_sites() | Project.objects.filter(
        status=CANCELLED_STATUS, is_deleted=False, is_test=False)


def _credited_or_no_site_q(prefix=''):
    """Rows on a credited site, or on no site at all. A subquery, not a list of ids, so the
    statement does not grow with the number of sites."""
    return (Q(**{f'{prefix}project__isnull': True})
            | Q(**{f'{prefix}project__in': _credited_sites()}))


def _fetch(lo, hi, lo_date, hi_date, today):
    """Every row both periods need, in ten queries. `lo`/`hi` bound the timestamps (already
    widened by BATCH_LOOKAROUND), `lo_date`/`hi_date` the attendance days."""
    # 1. Human activity rows, both periods plus the look-around. order_by() drops the
    #    model's newest-first ordering: the rows are sorted in Python for batching.
    #    entity_type rides along as a SIXTH column (RPT-B2a Q3): a blank-code row's kind is
    #    read from it, so labelling those rows costs no extra query. It is last so every
    #    r[0]..r[4] index _batches() and _merged() read is where it was.
    log_rows = list(ActivityLog.objects
                    .filter(human_activity_between(lo, hi))
                    .filter(_credited_or_no_site_q())
                    .order_by()
                    .values_list('pk', 'actor_id', 'action_code', 'timestamp', 'project_id',
                                 'entity_type'))

    # 2. UNPAIRED ledger rows only (pre-flight ruling 3). A row is paired when the same
    #    actor wrote any ActivityLog row on the same site within PAIR_WINDOW. Two EXISTS in
    #    one statement because SQL's NULL = NULL is not true: a site-less ledger row pairs
    #    only with a site-less log row, tested with isnull instead of equality.
    window = ActivityLog.objects.filter(
        actor_id=OuterRef('actor_id'),
        timestamp__gte=OuterRef('occurred_at') - PAIR_WINDOW,
        timestamp__lte=OuterRef('occurred_at') + PAIR_WINDOW)
    ledger_rows = list(StatusTransition.objects
                       .filter(actor__isnull=False, occurred_at__gte=lo, occurred_at__lt=hi)
                       .filter(_credited_or_no_site_q())
                       .annotate(on_site=Exists(window.filter(project_id=OuterRef('project_id'))),
                                 off_site=Exists(window.filter(project__isnull=True)))
                       .filter(Q(project__isnull=False, on_site=False)
                               | Q(project__isnull=True, off_site=False))
                       .order_by()
                       .values_list('pk', 'actor_id', 'subject_type', 'to_status',
                                    'occurred_at', 'project_id'))

    # 3. Attendance entries by their IST `date` (edge case 4: a late entry counts on the
    #    day it is for, not the day it was typed), voided ones out, as summary_queryset().
    attendance_rows = list(ContractorAttendance.objects
                           .filter(is_voided=False, date__gte=lo_date, date__lte=hi_date,
                                   project__in=_credited_sites())
                           .order_by()
                           .values_list('recorded_by_id', 'project_id', 'date', 'recorded_at'))

    # 4. Sign-ins, for "signed in, no action".
    login_rows = list(ActivityLog.objects
                      .filter(action_code=LOGIN_ACTION_CODE, actor__isnull=False,
                              timestamp__gte=lo, timestamp__lt=hi)
                      .order_by()
                      .values_list('actor_id', 'timestamp'))

    # 5. Open tasks per holder, AS OF NOW (D9): any type, not Done, human-owned, not N/A,
    #    on a live site. Overdue inside the same grouped count, by the PM card's rule.
    task_rows = list(Task.objects
                     .filter(assigned_to__isnull=False, status__in=OPEN_STATUSES,
                             phase__project__in=dashboard_live_sites())
                     .filter(human_owned_tasks_q() & applicable_tasks_q())
                     .values('assigned_to')
                     .annotate(open=Count('id'), overdue=Count('id', filter=card_overdue_q(today)))
                     .order_by()
                     .values_list('assigned_to', 'open', 'overdue'))

    # 6. Every person any row names, active on BOTH models (D11): one fetch, so a
    #    deactivated user simply has no profile here and drops out of every list.
    people = ({r[1] for r in log_rows} | {r[1] for r in ledger_rows}
              | {r[0] for r in attendance_rows} | {r[0] for r in login_rows}
              | {r[0] for r in task_rows}) - {None}
    profiles = {p.pk: p for p in UserProfile.objects
                .filter(pk__in=people, is_active=True, user__is_active=True)
                .select_related('user')}

    # 7. The credited sites any row touched (live ones and Cancelled ones), with what a row
    #    on a report shows. The rows were already limited to them, so this only labels them.
    site_ids = ({r[4] for r in log_rows} | {r[5] for r in ledger_rows}
                | {r[1] for r in attendance_rows}) - {None}
    sites = {}
    for p in (_credited_sites().filter(pk__in=site_ids).select_related('program')
              .only('id', 'site_code', 'project_id', 'site_name', 'customer_name',
                    'project_type', 'status', 'program__id', 'program__name',
                    'program__short_tender_code')):
        program = p.program
        sites[p.pk] = SiteInfo(
            id=p.pk, label=site_label(p), name=site_name(p), project_type=p.project_type,
            program_id=program.pk if program else None,
            program_label=(program.short_tender_code or program.name) if program else None,
            # Edge case 5: the status NOW decides, so a site activated between the two
            # periods is "in execution" in both. Its status on a past day is not stored.
            stage=(STAGE_CANCELLED if p.status == CANCELLED_STATUS
                   else STAGE_DESIGN if p.status == DRAFT_STATUS else STAGE_EXECUTION))

    # 8-10. Each open-task holder's latest action at any time, one grouped Max per source.
    #       All ledger rows, paired or not: a paired row's log twin is within 5 s of it.
    holders = [r[0] for r in task_rows if r[0] in profiles]
    last_ever = defaultdict(lambda: None)
    for source in (
        ActivityLog.objects.filter(actor_id__in=holders).filter(human_activity_q())
        .values('actor_id').annotate(m=Max('timestamp')).order_by().values_list('actor_id', 'm'),
        StatusTransition.objects.filter(actor_id__in=holders)
        .values('actor_id').annotate(m=Max('occurred_at')).order_by().values_list('actor_id', 'm'),
        ContractorAttendance.objects.filter(recorded_by_id__in=holders, is_voided=False)
        .values('recorded_by_id').annotate(m=Max('recorded_at')).order_by()
        .values_list('recorded_by_id', 'm'),
    ):
        for pk, moment in source:
            if last_ever[pk] is None or moment > last_ever[pk]:
                last_ever[pk] = moment

    return log_rows, ledger_rows, attendance_rows, login_rows, task_rows, profiles, sites, last_ever


# ---------------------------------------------------------------------------
# Bulk batches
# ---------------------------------------------------------------------------

def _batches(rows):
    """Split `rows` — (pk, actor_id, code, moment, site_id, ...) — into bulk batches.
    Only the first five columns are read; ActivityLog rows carry entity_type sixth.

    Returns [(actor_id, code, [row, ...]), ...]. Rows are grouped by (actor, code) and
    walked in time order; a gap above BULK_BATCH_GAP_SECONDS ends a run, and a run touching
    BULK_MIN_SITES or more distinct sites is a batch. Rows with no actor are never bulk:
    "one person" is the rule, and a system row has none.
    """
    groups = defaultdict(list)
    for row in rows:
        if row[1] is not None:
            groups[(row[1], row[2])].append(row)
    found = []
    for (actor, code), group in groups.items():
        group.sort(key=lambda r: (r[3], r[0]))
        run = [group[0]]
        for prev, cur in zip(group, group[1:]):
            if (cur[3] - prev[3]).total_seconds() <= BULK_BATCH_GAP_SECONDS:
                run.append(cur)
                continue
            found.append((actor, code, run))
            run = [cur]
        found.append((actor, code, run))
    return [(actor, code, run) for actor, code, run in found
            if len({r[4] for r in run} - {None}) >= BULK_MIN_SITES]


def _merged(log_batches, ledger_batches):
    """One bulk ACTION per screen use (B1 answer 1): batches by the same actor over the
    same set of sites starting within BULK_MERGE_SECONDS of each other are merged.
    design_bulk_allocate writes design_allocated AND design_qc_assigned for every site in
    the same POST; without this one use of the screen would read as two bulk actions.

    Returns [{'actor', 'codes', 'sources', 'members', 'first', 'last', 'site_ids'}], where
    `members` are the (source, pk) of every row in the merged batches. A merge never
    crosses sites: two screens used in the same second over different sites stay two.
    """
    batches = []
    for source, found in ((SOURCE_LOG, log_batches), (SOURCE_LEDGER, ledger_batches)):
        for actor, code, run in found:
            batches.append({'actor': actor, 'codes': [code], 'sources': [source],
                            'members': [(source, r[0]) for r in run], 'first': run[0][3],
                            'last': run[-1][3],
                            'site_ids': frozenset({r[4] for r in run} - {None})})
    # Same actor and same sites side by side, then by start, so a merge only ever compares
    # a batch with the group just before it.
    batches.sort(key=lambda b: (b['actor'], sorted(b['site_ids']), b['first']))
    merged = []
    for batch in batches:
        group = merged[-1] if merged else None
        if (group is not None and group['actor'] == batch['actor']
                and group['site_ids'] == batch['site_ids']
                and (batch['first'] - group['first']).total_seconds() < BULK_MERGE_SECONDS):
            for code in batch['codes']:
                if code not in group['codes']:
                    group['codes'].append(code)
            for source in batch['sources']:
                if source not in group['sources']:
                    group['sources'].append(source)
            group['members'].extend(batch['members'])
            group['last'] = max(group['last'], batch['last'])
            continue
        merged.append(batch)
    return merged


# ---------------------------------------------------------------------------
# One period from the fetched rows
# ---------------------------------------------------------------------------

def _period_activity(period, log_rows, ledger_rows, merged_batches,
                     attendance_rows, login_rows, task_rows, profiles, sites, last_ever):
    start, end = period.start_dt, period.end_dt

    def inside(moment):
        return start <= moment < end

    # Per actor: action count, sites, site-less actions, latest moment, and the count per
    # kind. The kind is counted HERE, beside the action itself (Q2: one counting path), so
    # a user's kinds can never disagree with their action count.
    actions = defaultdict(int)
    user_sites = defaultdict(set)
    no_site = defaultdict(int)
    latest = {}
    kinds = defaultdict(Counter)

    def act(actor, site_id, moment, kind):
        if actor is None:
            return
        actions[actor] += 1
        kinds[actor][kind] += 1
        if site_id is None:
            no_site[actor] += 1
        else:
            user_sites[actor].add(site_id)
        if actor not in latest or moment > latest[actor]:
            latest[actor] = moment

    bulk_pks = set()
    bulk = []
    bulk_sites_by_actor = defaultdict(set)
    # Every site a batch started in this period touched, whoever ran it: a deactivated
    # user's batch is in no list, but its sites are still not made active by it.
    batch_sites = set()
    for batch in merged_batches:
        bulk_pks.update(batch['members'])
        actor, first = batch['actor'], batch['first']
        # Edge case 3: a batch belongs to the period its FIRST row is in, wherever the
        # rest fall, so a batch over midnight is one action on the day it started.
        if not inside(first):
            continue
        run_sites = tuple(sorted(batch['site_ids'],
                                 key=lambda pk: (sites[pk].label if pk in sites else '', pk)))
        batch_sites.update(run_sites)
        if actor in profiles:
            bulk.append(BulkAction(actor_id=actor, actor_name=_person_name(profiles[actor]),
                                   action_code=' + '.join(sorted(batch['codes'])),
                                   source=' + '.join(sorted(batch['sources'])),
                                   started_at=first, ended_at=batch['last'],
                                   site_ids=run_sites))
            actions[actor] += 1
            kinds[actor][bulk_kind_label(batch['codes'])] += 1
            bulk_sites_by_actor[actor].update(run_sites)
            if actor not in latest or first > latest[actor]:
                latest[actor] = first

    # Non-bulk activity rows: actions for their actor, and activity for their site.
    site_actions = defaultdict(int)
    site_people = defaultdict(set)
    site_latest = {}
    for pk, actor, code, moment, site_id, entity_type in log_rows:
        if (SOURCE_LOG, pk) in bulk_pks or not inside(moment):
            continue
        act(actor if actor in profiles else None, site_id, moment,
            action_kind_label(code, entity_type))
        # A Cancelled site credits the user (act() above) but is never an active site.
        if site_id is not None and sites[site_id].stage != STAGE_CANCELLED:
            site_actions[site_id] += 1
            if actor in profiles:
                site_people[site_id].add(actor)
            if site_id not in site_latest or moment > site_latest[site_id]:
                site_latest[site_id] = moment

    # Unpaired ledger rows: actions only. They never make a site active, because D6's
    # active site is the ActivityLog rule site_breakdown() applies.
    for pk, actor, code, moment, site_id in ledger_rows:
        if (SOURCE_LEDGER, pk) in bulk_pks or not inside(moment):
            continue
        act(actor if actor in profiles else None, site_id, moment, ledger_kind_label(code))

    # Attendance: an action for whoever recorded it, on its date, timed by recorded_at.
    for actor, site_id, day, recorded_at in attendance_rows:
        if period.start_date <= day <= period.end_date and actor in profiles:
            act(actor, site_id, recorded_at, ATTENDANCE_KIND)

    def by_label(ids):
        return tuple(sorted(ids, key=lambda pk: (sites[pk].label if pk in sites else '', pk)))

    rows = []
    for actor, n in actions.items():
        profile = profiles[actor]
        mine = user_sites[actor]
        rows.append(UserActivity(
            profile_id=actor, name=_person_name(profile), role=profile.role or '—', actions=n,
            site_ids=by_label(mine),
            bulk_only_site_count=len(bulk_sites_by_actor[actor] - mine),
            actions_without_site=no_site[actor], last_action_at=latest[actor],
            kinds=tuple(sorted(kinds[actor].items(), key=lambda kv: (-kv[1], kv[0])))))
    rows.sort(key=lambda r: (-r.last_action_at.timestamp(), r.name.lower()))
    roles = defaultdict(lambda: [0, 0])
    for r in rows:
        roles[r.role][0] += 1
        roles[r.role][1] += r.actions
    active_users = ActiveUsers(
        rows=tuple(rows),
        by_role=tuple(RoleTotal(role, u, a) for role, (u, a) in sorted(roles.items())),
        users=len(rows), actions=sum(r.actions for r in rows),
        actions_without_site=sum(r.actions_without_site for r in rows))

    sign_ins = defaultdict(list)
    for actor, moment in login_rows:
        if inside(moment) and actor in profiles and actor not in actions:
            sign_ins[actor].append(moment)
    signed_in = sorted(
        (SignedInNoAction(profile_id=a, name=_person_name(profiles[a]),
                          role=profiles[a].role or '—', sign_ins=len(ms),
                          last_signed_in_at=max(ms)) for a, ms in sign_ins.items()),
        key=lambda r: (r.name.lower(), r.profile_id))

    idle = sorted(
        (OpenTasksNoActivity(profile_id=holder, name=_person_name(profiles[holder]),
                             role=profiles[holder].role or '—', open_tasks=n_open,
                             overdue_tasks=n_overdue, last_action_at=last_ever[holder])
         for holder, n_open, n_overdue in task_rows
         if holder in profiles and holder not in actions),
        key=lambda r: (-r.overdue_tasks, -r.open_tasks, r.name.lower()))

    site_rows = sorted(
        (SiteActivity(site=sites[pk], actions=n,
                      user_names=tuple(sorted(_person_name(profiles[a]) for a in site_people[pk])),
                      last_activity_at=site_latest[pk])
         for pk, n in site_actions.items() if pk in sites),
        key=lambda r: (-r.actions, r.site.label))
    active_sites = ActiveSites(
        rows=tuple(site_rows),
        in_execution=sum(1 for r in site_rows if r.site.stage == STAGE_EXECUTION),
        in_design=sum(1 for r in site_rows if r.site.stage == STAGE_DESIGN),
        bulk_only_site_ids=by_label(
            {pk for pk in batch_sites - set(site_actions)
             if pk in sites and sites[pk].stage != STAGE_CANCELLED}))

    bulk.sort(key=lambda b: (b.started_at, b.actor_name))
    return PeriodActivity(period=period, active_users=active_users,
                          signed_in_no_action=tuple(signed_in),
                          open_tasks_no_activity=tuple(idle), active_sites=active_sites,
                          bulk_actions=tuple(bulk), sites=sites)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def activity_report(period):
    """Everything above for `period` and `period.previous`, in ten queries."""
    windows = [period] + ([period.previous] if period.previous is not None else [])
    lo = min(p.start_dt for p in windows) - BATCH_LOOKAROUND
    hi = max(p.end_dt for p in windows) + BATCH_LOOKAROUND
    (log_rows, ledger_rows, attendance_rows, login_rows, task_rows, profiles, sites,
     last_ever) = _fetch(lo, hi, min(p.start_date for p in windows),
                         max(p.end_date for p in windows), timezone.localdate())

    log_batches = _batches(log_rows)
    # Ledger rows reshaped to the same (pk, actor, code, moment, site) form, the code
    # being what the row changed: a subject moving to a status.
    ledger = [(pk, actor, f'{subject}:{to_status}', moment, site)
              for pk, actor, subject, to_status, moment, site in ledger_rows]
    merged_batches = _merged(log_batches, _batches(ledger))

    def build(p):
        return _period_activity(p, log_rows, ledger, merged_batches,
                                attendance_rows, login_rows, task_rows, profiles, sites,
                                last_ever)

    return ActivityReport(current=build(period),
                          previous=build(period.previous) if period.previous else None)


def _compared(period, attr):
    report = activity_report(period)
    return Compared(current=getattr(report.current, attr),
                    previous=getattr(report.previous, attr) if report.previous else None)


def bulk_actions(period):
    """Compared(current, previous) tuples of BulkAction."""
    return _compared(period, 'bulk_actions')


def active_users(period):
    """Compared(current, previous) ActiveUsers."""
    return _compared(period, 'active_users')


def signed_in_no_action(period):
    """Compared(current, previous) tuples of SignedInNoAction."""
    return _compared(period, 'signed_in_no_action')


def open_tasks_no_activity(period):
    """Compared(current, previous) tuples of OpenTasksNoActivity (task counts as of now)."""
    return _compared(period, 'open_tasks_no_activity')


def active_sites(period):
    """Compared(current, previous) ActiveSites."""
    return _compared(period, 'active_sites')


#: How a profile with no role is shown, the same placeholder the activity rows use, so a
#: role-less account and a role-less active user land in the same "By role" row.
NO_ROLE = '—'


def accounts_by_role():
    """{role: active accounts} for every role that has one, in ONE query (RPT-B2a).

    Active is the D11 rule every report list uses: the profile AND the login are active
    (permissions._active_profiles() and _fetch()'s query 6 filter the same way). As of
    now: an account's past state is not stored. The "By role" table divides a period's
    active users by this, so the two must count the same people.
    """
    # Grouped in the database: one row per role, however many accounts there are.
    # order_by() drops UserProfile's default ordering, which would otherwise split groups.
    counts = defaultdict(int)
    for role, n in (UserProfile.objects.filter(is_active=True, user__is_active=True)
                    .values('role').annotate(n=Count('id')).order_by()
                    .values_list('role', 'n')):
        # Blank and NULL both read as NO_ROLE; summed, not overwritten, in case both occur.
        counts[role or NO_ROLE] += n
    return dict(counts)
