"""
Read-only report builders.

One function per report. Every function here is PURE: it reads through the ORM and
returns plain Python data. No writes, no email, no request object, no side effects.

Why a module rather than a place in views.py: the same figures are rendered twice —
once into the CEO section of the EOD digest email (a management command, no request)
and once onto a page (a view). A single builder is the only way those two surfaces
cannot drift apart. Same reasoning design_views.py records at its own head for a
self-contained subsystem.

SOFT DELETE: there are no custom managers anywhere in this codebase, so every queryset
below filters `is_deleted=False` itself — including through relation traversals
(`phase__project__is_deleted=False`). Task has no soft-delete column of its own; its
liveness comes entirely from its project.
"""

from datetime import datetime, time, timedelta

from django.db.models import BigIntegerField, Count, F, Max, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    SUBJECT_TASK, ActivityLog, Project, StatusTransition, Task, UserProfile,
)
from .utils import human_owned_tasks_q, applicable_tasks_q


# The active-project predicate, in one place. Deliberately identical to the one
# send_eod_digest.py already uses for its coordinator metrics, so the digest's two
# sections cannot disagree about which projects are live:
#   - not soft-deleted
#   - activated (activated_at set, which excludes Draft)
#   - not Cancelled
# Applied with a `project__`-style prefix so the same definition serves both the direct
# Project queries and the Task queries that reach a project through `phase__project`.
def _active_project_filter(prefix=''):
    """Return (filter_kwargs, exclude_kwargs) for the active-project predicate.

    `prefix` is the relation path to Project, e.g. '' for Project itself or
    'phase__project__' for Task. Returned as two dicts because 'not Cancelled' is an
    exclude, and Django has no `__ne` lookup.
    """
    return (
        {
            f'{prefix}is_deleted': False,
            f'{prefix}activated_at__isnull': False,
        },
        {f'{prefix}status': 'Cancelled'},
    )


def build_user_status_rows(report_date):
    """Per-user task and activity summary for the CEO daily report.
    Read-only. report_date is a date in Asia/Kolkata terms.

    Returns {'report_date': date, 'rows': [dict, ...], 'totals': dict}.

    Each row carries: profile, name, role, projects_assigned, tasks_assigned,
    not_started, in_progress, completed, blocked, overdue, done_today, done_by_others,
    closed_for_others, active_today, last_active.

    DONE TODAY is split by who did the work (the submitter where OPEX has an approval
    step, otherwise whoever marked it Done). done_today is the user's own tasks they
    finished themselves; done_by_others is their tasks someone else closed. The same
    tasks, seen from the other side, are the closer's closed_for_others, together with
    any unassigned task they closed. That is how a coordinator who holds no tasks but
    closes them for others gets a figure at all.

    LAST ACTIVE is the latest of the same three sources below, capped at the end of
    report_date: an aware datetime, or None if the user has no recorded activity up to
    then. Active Today is true exactly when last_active falls on report_date.

    Query count is CONSTANT at nine regardless of how many users exist — one grouped
    conditional aggregate, one grouped closed-for-others count, four id-pair sweeps, one
    profile fetch, and two grouped last-action sweeps for last_active / active_today.
    There is deliberately no per-user loop hitting the DB, matching the pattern
    send_eod_digest.py already documents for its own metrics.

    ACTIVE TODAY is true if ANY of three sources puts the user on report_date:
      a. `User.last_login` falls on that day
      b. at least one ActivityLog row names them as actor that day
      c. at least one StatusTransition row names them as actor that day
    It replaced a Logged In column that read (a) alone. `last_login` holds only the MOST
    RECENT login, so someone who logged in yesterday and worked all day today on the
    same session read as absent. (b) and (c) record actions, not page views: a user who
    only read the portal is not active, and the report's footnote says so.
    """
    # --- 1. Task metrics: ONE grouped conditional aggregate across every user ---------
    # Task has no soft-delete of its own, so the project carries the liveness filter.
    # `assigned_to__isnull=False` drops unassigned template rows, which belong to nobody
    # and would otherwise group under a NULL key.
    #
    # Counts every column on this report — tasks_assigned, the four status columns,
    # overdue, done_today — plus the task-derived half of projects_assigned below.
    # Mirrors are out because their status is written by another team's object, so a
    # mirror in this table would put another team's queue in this person's row.
    # Narrowing the BASE (rather than each Count) is what keeps the row-sum invariant
    # not_started + in_progress + completed + blocked == tasks_assigned true: the four
    # partition the same rows, before grouping, adding no join.
    #
    # N/A IS EXCLUDED ON THE BASE FOR EXACTLY THAT REASON, and it is the only place it
    # could go. The four status columns partition BY STATUS, and an N/A task still
    # holds one of the four stored values — so excluding it per-Count would drop it
    # from the four while `tasks_assigned=Count('id')` went on counting it, and the
    # invariant would break by precisely the number of N/A tasks. On the base the row
    # simply is not there, all six counts agree, and `DailyReportInvariantTests`,
    # `tests_progress_vs_workload` and `tests_mirror_metrics` pass untouched.
    # The `overdue` annotation inherits the exclusion here too, which is the whole of
    # what "N/A is never overdue" means on this report.
    task_active, task_cancelled = _active_project_filter('phase__project__')
    task_base = (
        Task.objects
        .filter(assigned_to__isnull=False, **task_active)
        .filter(human_owned_tasks_q())
        .filter(applicable_tasks_q())
        .exclude(**task_cancelled)
    )

    # WHO DID THE WORK on a completed task. Where there is an approval step (OPEX) it is
    # the person who submitted it: the approver is by rule someone else, so the Done
    # transition's actor would credit the reviewer, never the engineer. Otherwise it is
    # the actor of the task's latest transition to Done. submitted_by is written only by
    # the OPEX submit view and cleared on reject, so it is null on every Residential
    # task.
    #
    # A Done task with no ledger row has no doer. That is history, not a live path:
    # every path that marks a task Done now records its actor, but task completions
    # were first written to the ledger on 7 Sep 2026, and nothing before that has one.
    # Such a task stays with its assignee, as the column counted it before the split,
    # and credits nobody with Closed for Others. Reading it as "by others" would claim
    # someone else did every task completed before September.
    doer = Coalesce(
        'submitted_by',
        Subquery(
            StatusTransition.objects
            .filter(subject_type=SUBJECT_TASK, subject_id=OuterRef('pk'),
                    to_status=Task.DONE)
            .order_by('-occurred_at', '-pk')
            .values('actor_id')[:1]
        ),
        # The FK and the subquery's column resolve to different field types; both hold
        # a UserProfile pk.
        output_field=BigIntegerField(),
    )
    done_on_date = Q(status=Task.DONE, completed_at__date=report_date)

    # Status constants, never the literal strings — a renamed constant must break loudly
    # here rather than silently return zero.
    task_rows = (
        task_base
        # alias(), not annotate(): an annotation here would be selected and would join
        # the GROUP BY, splitting each user's row by doer and breaking the row-sum.
        .alias(doer_id=doer)
        .values('assigned_to')
        .annotate(
            tasks_assigned=Count('id'),
            not_started=Count('id', filter=Q(status=Task.NOT_STARTED)),
            in_progress=Count('id', filter=Q(status=Task.IN_PROGRESS)),
            completed=Count('id', filter=Q(status=Task.DONE)),
            blocked=Count('id', filter=Q(status=Task.BLOCKED)),
            # Overdue OVERLAPS the four status columns by design — an overdue task is
            # still Not Started / In Progress / Blocked. due_date is nullable and
            # `__lt` already excludes NULL, so undated tasks never count as overdue.
            overdue=Count('id', filter=Q(due_date__lt=report_date) & ~Q(status=Task.DONE)),
            # completed_at is a DateTimeField; the `__date` lookup is timezone-aware
            # under USE_TZ=True and resolves against TIME_ZONE (Asia/Kolkata), which is
            # the same IST calendar day report_date is expressed in.
            #
            # status=Done as well, because completed_at alone is not enough: the human
            # status path never clears completed_at when a task leaves Done, so a task
            # completed and reopened on the same day would still count. Done Today
            # overlaps Completed and sits outside the row-sum, so this does not move it.
            #
            # Split by who did the work. done_today is the user's own; done_by_others is
            # their task closed by someone else (a PM, a coordinator, or a milestone
            # receipt). Together they are every task of theirs completed that day. An
            # unknown doer counts as the user's own (see the doer comment above); the
            # isnull test is explicit because NOT (NULL = x) is NULL in SQL, not true.
            done_today=Count('id', filter=done_on_date & (
                Q(doer_id__isnull=True) | Q(doer_id=F('assigned_to')))),
            done_by_others=Count('id', filter=done_on_date & Q(doer_id__isnull=False)
                                 & ~Q(doer_id=F('assigned_to'))),
            projects_via_tasks=Count('phase__project', distinct=True),
        )
    )
    metrics_by_profile = {row['assigned_to']: row for row in task_rows}

    # --- 1b. Tasks each user closed on someone else's behalf: ONE grouped query ------
    # The other side of done_by_others, credited to the person who did the work. This is
    # what makes a coordinator's effort visible: they hold no tasks, so every column
    # above reads zero for them however many tasks they closed. Counts tasks assigned
    # to someone else AND tasks assigned to nobody. Same liveness, mirror and
    # applicability rules as task_base, minus its assigned_to filter — the three
    # filters are repeated rather than shared because this queryset deliberately
    # drops one of task_base's four and copying the remaining three is clearer than
    # building task_base in two stages.
    closed_for_others_by_profile = dict(
        Task.objects
        .filter(**task_active)
        .filter(human_owned_tasks_q())
        .filter(applicable_tasks_q())
        .exclude(**task_cancelled)
        .filter(done_on_date)
        .annotate(doer_id=doer)
        .filter(doer_id__isnull=False)
        .filter(Q(assigned_to__isnull=True) | ~Q(assigned_to=F('doer_id')))
        .values('doer_id')
        .annotate(n=Count('id'))
        .values_list('doer_id', 'n')
    )

    # --- 2. Project sets per user ----------------------------------------------------
    # "Projects Assigned" is the UNION of four sources, deduplicated per user: the three
    # direct links on Project plus every project the user holds a task on. The union
    # happens in Python because a single ORM query joining all four would fan rows out
    # and inflate every other column in the aggregate above.
    project_ids_by_profile = {}

    def _collect(pairs):
        for profile_id, project_id in pairs:
            if profile_id is None or project_id is None:
                continue
            project_ids_by_profile.setdefault(profile_id, set()).add(project_id)

    # Task-derived half. This is the ONLY source of projects for a Site Engineer:
    # Project.assigned_site_engineer was removed in migration 0037, so a site engineer's
    # relationship to a project is "holds a task on it" and nothing else. Intended.
    _collect(
        task_base.values_list('assigned_to', 'phase__project').distinct()
    )

    proj_active, proj_cancelled = _active_project_filter()
    project_base = Project.objects.filter(**proj_active).exclude(**proj_cancelled)

    # Direct half — three FK/M2M links, each a flat (profile_id, project_id) sweep.
    _collect(
        project_base.filter(assigned_pm__isnull=False)
        .values_list('assigned_pm', 'id')
    )
    _collect(
        project_base.filter(assigned_design__isnull=False)
        .values_list('assigned_design', 'id')
    )
    # M2M traversal yields one row per (project, coordinator) pair — exactly the shape
    # _collect wants, and the reason this is its own query rather than a join above.
    _collect(
        project_base.filter(coordinators__isnull=False)
        .values_list('coordinators', 'id')
    )

    # --- 3. Rows -------------------------------------------------------------------
    # Anyone appearing in EITHER source is a candidate. No role is excluded: this report
    # is about who holds work, and every role that can hold work belongs in it.
    # (EOD_DIGEST_EXCLUDED_ROLES governs who RECEIVES the individual digest — a
    # different question entirely, and not applicable here.)
    # Anyone who closed a task for someone else is a candidate too, so that work shows
    # even for a user with no task and no project link of their own.
    candidate_ids = (set(metrics_by_profile) | set(project_ids_by_profile)
                     | set(closed_for_others_by_profile))
    if not candidate_ids:
        return {'report_date': report_date, 'rows': [], 'totals': _empty_totals()}

    profiles = (
        UserProfile.objects
        .filter(pk__in=candidate_ids, is_active=True, user__is_active=True)
        .select_related('user')
    )

    # --- 3a. When each user last acted, up to the end of report_date --------------
    # ONE grouped Max per source. The day is local midnight to the next local midnight
    # in TIME_ZONE (Asia/Kolkata), built as aware datetimes, so a 00:30 IST action is
    # not read as the previous UTC day. Everything is capped at day_end, so a past
    # report_date never shows activity that happened after it. Both actor FKs point at
    # UserProfile, so the ids compare directly with profile.pk; no mapping from User is
    # needed. A null actor on StatusTransition is the system (a derivation, not a
    # person) and is left out.
    day_start = timezone.make_aware(datetime.combine(report_date, time.min))
    day_end = timezone.make_aware(datetime.combine(report_date + timedelta(days=1), time.min))
    last_action_by_profile = dict(
        ActivityLog.objects
        .filter(actor_id__in=candidate_ids, timestamp__lt=day_end)
        .values('actor_id').annotate(last=Max('timestamp'))
        .values_list('actor_id', 'last')
    )
    for actor_id, last in (
        StatusTransition.objects
        .filter(actor_id__in=candidate_ids, occurred_at__lt=day_end)
        .values('actor_id').annotate(last=Max('occurred_at'))
        .values_list('actor_id', 'last')
    ):
        if actor_id not in last_action_by_profile or last > last_action_by_profile[actor_id]:
            last_action_by_profile[actor_id] = last

    rows = []
    for profile in profiles:
        metrics = metrics_by_profile.get(profile.pk, {})
        user = profile.user
        # last_login stays as a source on purpose. The login signal wraps its
        # ActivityLog write in `except Exception: pass`, so a 'user_login' row can be
        # missing when the login happened; last_login is written by Django's own auth
        # machinery. It holds only the MOST RECENT login, so for a past report_date a
        # later login is ignored rather than read as activity on that day.
        candidates = [last_action_by_profile.get(profile.pk)]
        if user.last_login is not None and user.last_login < day_end:
            candidates.append(user.last_login)
        last_active = max((c for c in candidates if c is not None), default=None)
        # Active Today follows from last_active: every source is already capped at
        # day_end, so the latest one falling on or after day_start is the whole test.
        active_today = last_active is not None and last_active >= day_start

        rows.append({
            'profile':           profile,
            'name':              user.get_full_name() or user.username,
            'role':              profile.role or '—',
            'projects_assigned': len(project_ids_by_profile.get(profile.pk, ())),
            'tasks_assigned':    metrics.get('tasks_assigned', 0),
            'not_started':       metrics.get('not_started', 0),
            'in_progress':       metrics.get('in_progress', 0),
            'completed':         metrics.get('completed', 0),
            'blocked':           metrics.get('blocked', 0),
            'overdue':           metrics.get('overdue', 0),
            'done_today':        metrics.get('done_today', 0),
            'done_by_others':    metrics.get('done_by_others', 0),
            'closed_for_others': closed_for_others_by_profile.get(profile.pk, 0),
            'active_today':      active_today,
            'last_active':       last_active,
        })

    # Most recently active first, by last_active (capped at the end of report_date).
    # Users with no recorded activity at all go last. Name breaks ties so the order is
    # stable run to run. Compared to the MINUTE, the precision the report displays:
    # two users shown at the same time are then in name order, rather than ordered by
    # seconds nobody can see.
    rows.sort(key=lambda r: (
        r['last_active'] is None,
        -(int(r['last_active'].timestamp()) // 60) if r['last_active'] is not None else 0,
        r['name'].lower(),
    ))

    # --- 4. Totals -----------------------------------------------------------------
    totals = _empty_totals()
    for row in rows:
        for key in _NUMERIC_COLUMNS:
            totals[key] += row[key]
        if not row['active_today']:
            totals['not_active_count'] += 1
    totals['user_count'] = len(rows)

    return {'report_date': report_date, 'rows': rows, 'totals': totals}


# Every numeric column the totals row sums. NOTE on `projects_assigned`: this is a sum
# of per-user counts, NOT a count of distinct projects — one project with a PM and two
# coordinators contributes 3. That is the correct total for a "workload across people"
# column and is what the per-row figures add up to; it is not a portfolio size.
_NUMERIC_COLUMNS = (
    'projects_assigned', 'tasks_assigned', 'not_started', 'in_progress',
    'completed', 'blocked', 'overdue', 'done_today', 'done_by_others',
    'closed_for_others',
)


def _empty_totals():
    totals = {key: 0 for key in _NUMERIC_COLUMNS}
    totals['not_active_count'] = 0
    totals['user_count'] = 0
    return totals
