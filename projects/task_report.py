"""
The daily morning task report: who is told about which open tasks.

PURE AND READ-ONLY. Every function here reads through the ORM and returns plain Python
data. No writes, no sending, no request object. The management command
send_morning_task_report renders and sends what build_task_reports() returns; keeping
the figures here means a test can assert them without sending anything.

WHICH TASKS (report_tasks_q):
  - assigned to an ACTIVE person (UserProfile.is_active and User.is_active). A task
    held by someone who has left is nobody's morning task; it is not in any count,
    including a PM's "your projects and tasks" figure.
  - open: status in task_health.OPEN_STATUSES (every status but Done; Blocked counts)
  - not Not Applicable (Task.is_not_applicable is a flag, not a status)
  - not a mirror (utils.human_owned_tasks_q), as build_user_status_rows does: a mirror
    is moved by another team's object, so it is nobody's to-do
  - Internal AND External: a DISCOM follow-up is still somebody's task this morning.
    The email says this makes the counts higher than the CEO Tasks card's (Internal).
  - on a project that is live: not deleted, activated, status Active or In Progress,
    not is_test. Active / In Progress is the stat blocks' and tasks_drill_down's
    predicate, which is the page the email's link opens. It is deliberately NOT
    reports._active_project_filter, which also admits On Hold and Commissioned.

THE THREE COUNTS:
  Delayed       task_health.overdue_q(report_date): due date strictly before today
  Due today     due_today_q(report_date): due date == today, the same open-task terms
                as the overdue rule (task_health has no due-today form of its own)
  No due date   due_date is null

WHO GETS WHICH SCOPE. Each person gets exactly ONE, the first rule that matches:
  1. role CEO / Admin / System Admin       -> "all active projects"
  2. manages a project (PM or coordinator,  -> "your projects and tasks": every
     via permissions.managed_project_ids_      in-scope task on those projects, PLUS
     by_profile)                               their own in-scope tasks on any other
                                               counted project. Each task once.
  3. holds at least one in-scope task       -> "your tasks"
A person whose three counts are all zero is not in the result at all.

QUERY COUNT IS FIXED AT FIVE, whatever the number of users: recipients (1), managed
projects (2), one grouped count, one list of every delayed / due-today task. Each
person's figures are then cut from those in Python.

`report_date` IS ALWAYS PASSED IN, from the caller's timezone.localdate() (IST).
"""
from django.db.models import Count, Q
from django.urls import reverse

from .forms import TYPED_DATE_FLOOR
from .models import Task, UserProfile
from .permissions import managed_project_ids_by_profile
from .task_health import OPEN_STATUSES, days_overdue, is_overdue, overdue_q
from .utils import applicable_tasks_q, human_owned_tasks_q


#: Roles whose report covers every live project. They are portfolio-wide by remit (the
#: same three permissions.CEO_DASHBOARD_ROLES names), so "your tasks" would tell them
#: nothing about the company.
ALL_PROJECTS_ROLES = frozenset({'CEO', 'Admin', 'System Admin'})

#: The project statuses a task must be on. The stat blocks' and tasks_drill_down's set.
REPORT_PROJECT_STATUSES = ('Active', 'In Progress')

SCOPE_ALL = 'all active projects'
#: A manager's scope: the projects they manage, and their own tasks anywhere else. The
#: words are sent as they are, in the email and as a WhatsApp parameter.
SCOPE_PROJECTS = 'your projects and tasks'
SCOPE_TASKS = 'your tasks'

#: Rows in each of the two lists. The counts above them are never capped.
LIST_LIMIT = 20

#: What the Days late column says for a due date before the typed-date floor: a
#: mistyped year, not a real delay. The task still counts as delayed.
CHECK_DATE = 'check date'


def report_tasks_q():
    """Q() selecting every task the morning report can count. See the module docstring."""
    return (
        Q(
            assigned_to__isnull=False,
            assigned_to__is_active=True,
            assigned_to__user__is_active=True,
            status__in=OPEN_STATUSES,
            is_not_applicable=False,
            phase__project__is_deleted=False,
            phase__project__activated_at__isnull=False,
            phase__project__status__in=REPORT_PROJECT_STATUSES,
            phase__project__is_test=False,
        )
        & human_owned_tasks_q()
        & applicable_tasks_q()
    )


def due_today_q(report_date):
    """Q() matching tasks due on report_date that are still open: the same two "still
    open" terms task_health.overdue_q() uses, so a Done or Not Applicable task due today
    is never counted."""
    return Q(due_date=report_date, status__in=OPEN_STATUSES, is_not_applicable=False)


def report_link_path(delayed, due_today):
    """The path the report links to. The page whose list is most worth opening: the
    overdue list when anything is late, else the due-today list, else the root, which
    lands a logged-in user on their own dashboard. No page was built for this report,
    so the linked list can differ a little from the counts (the email says so)."""
    if delayed:
        return reverse('tasks_overdue')
    if due_today:
        return reverse('tasks_due_today')
    return '/'


def _in_scope(scope, profile_pk, projects, project_pk, assignee_pk):
    """Does a task on `project_pk` held by `assignee_pk` belong in this person's report?"""
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_PROJECTS:
        # A UNION, so a manager's own task on a project they do not manage is still
        # theirs this morning. Each task is one row (or one grouped cell), and the test
        # is an OR on that row, so a task that is both on their project and their own is
        # counted once.
        return project_pk in projects or assignee_pk == profile_pk
    return assignee_pk == profile_pk


def _person_name(profile):
    return profile.user.get_full_name() or profile.user.username


def _row(task, report_date):
    """One table row, from a task already loaded with its project and assignee."""
    late = days_overdue(task, report_date)
    if late and task.due_date < TYPED_DATE_FLOOR:
        late = CHECK_DATE
    return {
        'project': task.phase.project.project_id,
        'task': task.task_name,
        'assigned_to': _person_name(task.assigned_to),
        'due_date': task.due_date,
        'days_late': late,
    }


def build_task_reports(report_date):
    """Every recipient of the morning report on `report_date`, in username order.

    Returns a list of dicts, one per person with at least one non-zero count:
        profile, first_name, role, scope, show_assignee,
        delayed, due_today, no_due_date             (int, never capped)
        delayed_tasks                               (oldest due date first, <= 20 rows)
        due_today_tasks                             (by project then task order, <= 20)
        link_path
    Each list row is {project, task, assigned_to, due_date, days_late}.

    Five queries in total, whatever the number of users (see the module docstring).
    """
    # 1. Everyone who can receive anything: active profile of an active user. The user
    #    is joined for the name and the username the command filters --only-user on.
    recipients = list(
        UserProfile.objects
        .filter(is_active=True, user__is_active=True)
        .select_related('user')
        .order_by('user__username')
    )

    # 2-3. {profile pk: managed project pks}, two queries for the whole company.
    managed = managed_project_ids_by_profile()

    scoped = Task.objects.filter(report_tasks_q())

    # 4. The three counts, grouped by (project, assignee). Every scope is a sum over
    #    these cells: all of them, a manager's projects, or one assignee's. order_by()
    #    clears any default ordering so it cannot join the GROUP BY.
    cells = list(
        scoped
        .values('phase__project_id', 'assigned_to_id')
        .annotate(
            delayed=Count('pk', filter=overdue_q(report_date)),
            due_today=Count('pk', filter=due_today_q(report_date)),
            no_due_date=Count('pk', filter=Q(due_date__isnull=True)),
        )
        .order_by()
    )

    # 5. Every delayed or due-today task in scope, oldest due date first, with the
    #    project and the assignee joined so building a row costs nothing more. Each
    #    person's two lists are cut from this one list. It is every such task, not 20,
    #    because which 20 depends on the person.
    listed = list(
        scoped
        .filter(overdue_q(report_date) | due_today_q(report_date))
        .select_related('phase__project', 'assigned_to__user')
        .order_by('due_date', 'phase__project__project_id', 'task_order', 'pk')
    )

    reports = []
    for profile in recipients:
        # Scope precedence: the first rule that matches, so a PM who also holds tasks
        # gets one report ("your projects and tasks"), never two.
        projects = managed.get(profile.pk, frozenset())
        if profile.role in ALL_PROJECTS_ROLES:
            scope = SCOPE_ALL
        elif projects:
            scope = SCOPE_PROJECTS
        else:
            scope = SCOPE_TASKS

        delayed = due_today = no_due_date = 0
        for cell in cells:
            if _in_scope(scope, profile.pk, projects,
                         cell['phase__project_id'], cell['assigned_to_id']):
                delayed += cell['delayed']
                due_today += cell['due_today']
                no_due_date += cell['no_due_date']
        if not (delayed or due_today or no_due_date):
            continue

        delayed_tasks, due_today_tasks = [], []
        for task in listed:
            if not _in_scope(scope, profile.pk, projects,
                             task.phase.project_id, task.assigned_to_id):
                continue
            # Partitioned with task_health's row form of the same rule the query used,
            # so a task lands in the list its count put it in.
            if is_overdue(task, report_date):
                if len(delayed_tasks) < LIST_LIMIT:
                    delayed_tasks.append(_row(task, report_date))
            elif len(due_today_tasks) < LIST_LIMIT:
                due_today_tasks.append(_row(task, report_date))

        reports.append({
            'profile': profile,
            'first_name': profile.user.first_name or profile.user.username,
            'role': profile.role,
            'scope': scope,
            # "Assigned to" only says something when the tasks can be other people's.
            'show_assignee': scope != SCOPE_TASKS,
            'delayed': delayed,
            'due_today': due_today,
            'no_due_date': no_due_date,
            'delayed_tasks': delayed_tasks,
            'due_today_tasks': due_today_tasks,
            'link_path': report_link_path(delayed, due_today),
        })
    return reports
