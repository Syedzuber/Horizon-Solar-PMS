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

from django.db.models import Count, Q
from django.utils import timezone

from .models import Project, Task, UserProfile


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
    """Per-user task and login summary for the CEO daily report.
    Read-only. report_date is a date in Asia/Kolkata terms.

    Returns {'report_date': date, 'rows': [dict, ...], 'totals': dict}.

    Each row carries: profile, name, role, projects_assigned, tasks_assigned,
    not_started, in_progress, completed, blocked, overdue, done_today, logged_in.

    Query count is CONSTANT at six regardless of how many users exist — one grouped
    conditional aggregate, four id-pair sweeps, one profile fetch. There is deliberately
    no per-user loop hitting the DB, matching the pattern send_eod_digest.py already
    documents for its own metrics.

    LOGGED IN uses `User.last_login`, not ActivityLog's 'user_login' rows. The login
    signal at signals.py:28-43 wraps its write in `except Exception: pass`, so an
    ActivityLog login can be silently missing; `last_login` is written by Django's own
    auth machinery. The consequence, stated plainly: `last_login` holds only the MOST
    RECENT login, so for a past report_date this column answers "was their latest login
    on that day", not "did they log in at all that day". It is exact for today.
    """
    # --- 1. Task metrics: ONE grouped conditional aggregate across every user ---------
    # Task has no soft-delete of its own, so the project carries the liveness filter.
    # `assigned_to__isnull=False` drops unassigned template rows, which belong to nobody
    # and would otherwise group under a NULL key.
    task_active, task_cancelled = _active_project_filter('phase__project__')
    task_base = (
        Task.objects
        .filter(assigned_to__isnull=False, **task_active)
        .exclude(**task_cancelled)
    )

    # Status constants, never the literal strings — a renamed constant must break loudly
    # here rather than silently return zero.
    task_rows = (
        task_base
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
            done_today=Count('id', filter=Q(completed_at__date=report_date)),
            projects_via_tasks=Count('phase__project', distinct=True),
        )
    )
    metrics_by_profile = {row['assigned_to']: row for row in task_rows}

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
    candidate_ids = set(metrics_by_profile) | set(project_ids_by_profile)
    if not candidate_ids:
        return {'report_date': report_date, 'rows': [], 'totals': _empty_totals()}

    profiles = (
        UserProfile.objects
        .filter(pk__in=candidate_ids, is_active=True, user__is_active=True)
        .select_related('user')
    )

    rows = []
    for profile in profiles:
        metrics = metrics_by_profile.get(profile.pk, {})
        user = profile.user
        # last_login is stored UTC-aware; localtime() puts it in IST before the date
        # comparison, so a 01:00 IST login is not read as the previous UTC day.
        logged_in = False
        if user.last_login is not None:
            logged_in = timezone.localtime(user.last_login).date() == report_date

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
            'logged_in':         logged_in,
        })

    # Busiest-and-absent first: most overdue at the top, and within the same overdue
    # count the people who have not logged in today lead, because that is the pairing
    # a CEO acts on. Name breaks the remaining ties so the order is stable run to run.
    rows.sort(key=lambda r: (-r['overdue'], r['logged_in'], r['name'].lower()))

    # --- 4. Totals -----------------------------------------------------------------
    totals = _empty_totals()
    for row in rows:
        for key in _NUMERIC_COLUMNS:
            totals[key] += row[key]
        if not row['logged_in']:
            totals['not_logged_in_count'] += 1
    totals['user_count'] = len(rows)

    return {'report_date': report_date, 'rows': rows, 'totals': totals}


# Every numeric column the totals row sums. NOTE on `projects_assigned`: this is a sum
# of per-user counts, NOT a count of distinct projects — one project with a PM and two
# coordinators contributes 3. That is the correct total for a "workload across people"
# column and is what the per-row figures add up to; it is not a portfolio size.
_NUMERIC_COLUMNS = (
    'projects_assigned', 'tasks_assigned', 'not_started', 'in_progress',
    'completed', 'blocked', 'overdue', 'done_today',
)


def _empty_totals():
    totals = {key: 0 for key in _NUMERIC_COLUMNS}
    totals['not_logged_in_count'] = 0
    totals['user_count'] = 0
    return totals
