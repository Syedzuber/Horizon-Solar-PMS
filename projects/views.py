import json
import logging
import re
import uuid as _uuid
from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse as _urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Exists, Max, Min, OuterRef, Prefetch, Q, Sum
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import (
    UserProfile, Project, ProjectPhase, Task, DueDateChangeLog,
    Vendor, VendorCategory, VendorBrand,
    BOQ, BOQItem, BOQRevision, Notification, get_standard_boq_items,
    PaymentMilestone, ProjectDocument, TaskAttachment,
    Issue, ActivityLog, Comment, log_activity,
    DeliveryChallan, DCLineItem, recalculate_dc_status, get_material_status,
    PaymentRequest, NotificationLog, SystemSettings, DesignSubmission,
)
from .notifications import send_notification, send_raw_email
from .forms import UserCreateForm, UserEditForm, AdminUserEditForm, ProjectCreateForm, ProjectEditForm, TaskAddForm, VendorForm
from .decorators import login_required, role_required, get_user_dashboard
from .utils import attach_residential_template, calculate_due_dates, recalculate_from_task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File upload constants
# ---------------------------------------------------------------------------

ALLOWED_DOCUMENT_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx']
ALLOWED_PHOTO_EXTENSIONS    = ['jpg', 'jpeg', 'png']
ALLOWED_EXTENSIONS          = ALLOWED_DOCUMENT_EXTENSIONS + ALLOWED_PHOTO_EXTENSIONS
MAX_FILE_SIZE_BYTES         = 20 * 1024 * 1024  # 20 MB

MIME_TYPE_MAP = {
    'pdf':  'application/pdf',
    'doc':  'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls':  'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_view(request):
    """
    Render login form and authenticate. Redirects already-authenticated users
    to their role dashboard immediately. Access: public.
    """
    if request.user.is_authenticated:
        return redirect(get_user_dashboard(request.user))

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect(get_user_dashboard(user))
        messages.error(request, 'Invalid username or password')
        return render(request, 'registration/login.html')

    return render(request, 'registration/login.html')


def logout_view(request):
    """Log out the current user and redirect to login page. Access: any authenticated user."""
    logout(request)
    return redirect('/login/')


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

@login_required
def dashboard_admin(request):
    """Admin landing page. Access: any authenticated user (no role restriction here — Admin nav is in the template)."""
    return render(request, 'dashboard/admin.html')


def _project_material_badge(project_id, delivery_lookup):
    """
    Compute the PM dashboard Materials badge value from a pre-fetched delivery_lookup dict.
    Called inside the projects_with_progress loop — lookup was built from a single query
    before the loop to avoid N+1 (one query per project would be too expensive).
    Returns 'Pending', 'Partial', or 'Received'.
    """
    project_data = delivery_lookup.get(project_id, {})
    if not project_data:
        return 'Pending'

    categories = ['Solar Modules', 'Structure', 'Inverter', 'BOS']
    statuses = []
    for cat in categories:
        cat_data = project_data.get(cat)
        if not cat_data:
            statuses.append('Pending')
            continue
        received = cat_data['total_received'] or 0
        ordered  = cat_data['total_ordered'] or 0
        has_damage = cat_data['has_damage']
        if received == 0:
            statuses.append('Pending')
        elif received >= ordered and not has_damage:
            statuses.append('Received')
        else:
            statuses.append('Partial')

    if all(s == 'Pending' for s in statuses):
        return 'Pending'
    elif all(s == 'Received' for s in statuses):
        return 'Received'
    else:
        return 'Partial'


def _build_delivery_lookup(project_ids):
    """
    Build a per-project per-category aggregation dict from DCLineItem data.
    Single query across all given project IDs — avoids N+1 in multi-project dashboard loops.
    Returns: {project_id: {category: {'total_ordered', 'total_received', 'has_damage'}}}
    Called by both dashboard_pm and dashboard_scm — shared to prevent independent
    implementations drifting apart (the root cause of the Q4 representation-5 drift).
    """
    delivery_rows = DCLineItem.objects.filter(
        challan__project__project_id__in=project_ids
    ).values(
        'challan__project__project_id', 'boq_category'
    ).annotate(
        total_ordered=Sum('ordered_quantity'),
        total_received=Sum('received_quantity'),
        # Use damaged_quantity (precise numeric field) — not the coarse condition string
        damage_count=Count('pk', filter=Q(damaged_quantity__gt=0)),
    )
    delivery_lookup = {}
    for row in delivery_rows:
        pid = row['challan__project__project_id']
        cat = row['boq_category']
        if pid not in delivery_lookup:
            delivery_lookup[pid] = {}
        delivery_lookup[pid][cat] = {
            'total_ordered':  row['total_ordered'] or 0,
            'total_received': row['total_received'] or 0,
            'has_damage':     row['damage_count'] > 0,
        }
    return delivery_lookup


_ROLE_DASHBOARD = {
    'PM':           'dashboard_pm',
    'Design':       'dashboard_design',
    'SCM':          'dashboard_scm',
    'Site Engineer': 'dashboard_site_engineer',
    'Finance':       'dashboard_finance',
    'BD / Sales':    'dashboard_bd',
    'CEO':           'dashboard_ceo',
}

_FILTER_TITLES = {
    'due-today': 'Tasks Due Today',
    'due-soon':  'Tasks Due in Next 7 Days',
    'overdue':   'Overdue Tasks',
}


@login_required
def tasks_drill_down(request, filter_type):
    """Read-only list of tasks grouped by project, filtered by due-date window.
    Scoping mirrors the logged-in user's dashboard (PM/Design/SCM/SE/etc.)."""
    if filter_type not in _FILTER_TITLES:
        raise Http404

    today   = date.today()
    profile = request.user.profile
    role    = profile.role

    base_qs = Task.objects.filter(
        phase__project__is_deleted=False,
        phase__project__status__in=['Active', 'In Progress'],
        due_date__isnull=False,
    ).select_related('phase__project')

    if role == 'PM':
        base_qs = base_qs.filter(phase__project__assigned_pm=profile)
    elif role == 'Design':
        base_qs = base_qs.filter(phase__project__assigned_design=profile)
    elif role == 'Site Engineer':
        base_qs = base_qs.filter(phase__project__assigned_site_engineer=profile)
    # SCM and others: all active non-deleted projects

    active_statuses = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]

    if filter_type == 'due-today':
        tasks_qs = base_qs.filter(due_date=today, status__in=active_statuses)
    elif filter_type == 'due-soon':
        tasks_qs = base_qs.filter(
            due_date__gt=today,
            due_date__lte=today + timedelta(days=7),
            status__in=active_statuses,
        )
    else:  # overdue
        tasks_qs = base_qs.filter(due_date__lt=today).exclude(status=Task.DONE)

    project_map = {}
    for task in tasks_qs.order_by('phase__project__project_id', 'due_date'):
        proj = task.phase.project
        if proj.project_id not in project_map:
            project_map[proj.project_id] = {'project': proj, 'tasks': []}
        project_map[proj.project_id]['tasks'].append(task)

    groups       = list(project_map.values())
    total_count  = sum(len(g['tasks']) for g in groups)
    back_url     = reverse(_ROLE_DASHBOARD.get(role, 'dashboard_pm'))

    return render(request, 'tasks/task_drill_down.html', {
        'page_title':    _FILTER_TITLES[filter_type],
        'filter_type':   filter_type,
        'groups':        groups,
        'total_count':   total_count,
        'project_count': len(groups),
        'today':         today,
        'back_url':      back_url,
    })


@login_required
@role_required(['PM'])
def dashboard_pm(request):
    """
    PM dashboard: summary cards + per-project progress + task lists.
    All queries scoped to the PM's own projects only.
    Access: PM only.
    TODO: the per-project loop runs multiple queries per project — consider
    annotating with Subquery or moving to prefetch_related before scaling.
    """
    pm_profile = request.user.profile

    # Summary card counts — each is a single COUNT query
    active_projects = Project.objects.filter(
        assigned_pm=pm_profile,
        status__in=['Active', 'In Progress'],
    ).count()

    due_today = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        due_date=date.today(),
        due_date__isnull=False,
        task_type=Task.INTERNAL,
        status__in=['Not Started', 'In Progress'],
    ).count()

    blocked_tasks = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        status='Blocked',
    ).count()

    pending_approvals = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        assigned_role=Task.PM,
        status='Not Started',
    ).count()

    external_pending = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        task_type=Task.EXTERNAL,
        status__in=['Not Started', 'In Progress'],
    ).count()

    # Draft projects assigned to this PM (Zoho-created or manually created and not yet activated)
    draft_projects = list(
        Project.objects.filter(
            assigned_pm=pm_profile,
            status='Draft',
            is_deleted=False,
        ).order_by('-created_at')
    )

    projects_with_progress = []
    for project in Project.objects.filter(assigned_pm=pm_profile, status__in=['Active', 'In Progress'], is_deleted=False):
        try:
            project.boq_status = project.boq.status
            project.boq_url    = f'/projects/{project.project_id}/boq/'
        except Exception:
            project.boq_status = None
            project.boq_url    = None

        total_tasks    = Task.objects.filter(phase__project=project).count()
        done_tasks     = Task.objects.filter(phase__project=project, status='Done').count()
        internal_total = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL).count()
        internal_done  = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL, status='Done').count()
        internal_percent = int(internal_done / internal_total * 100) if internal_total else 0
        ext_pending    = Task.objects.filter(
            phase__project=project, task_type=Task.EXTERNAL,
            status__in=['Not Started', 'In Progress'],
        ).count()
        overdue_count  = Task.objects.filter(
            phase__project=project, task_type=Task.INTERNAL,
            due_date__lt=date.today(), due_date__isnull=False,
            status__in=['Not Started', 'In Progress'],
        ).count()
        blocked_count = Task.objects.filter(
            phase__project=project, status='Blocked',
        ).count()
        blocked_tasks_for_project = list(
            Task.objects.filter(phase__project=project, status='Blocked')
            .select_related('phase')[:5]
        )
        overdue_tasks_for_project = list(
            Task.objects.filter(
                phase__project=project, task_type=Task.INTERNAL,
                due_date__lt=date.today(), due_date__isnull=False,
                status__in=['Not Started', 'In Progress'],
            ).select_related('phase')[:5]
        )
        is_delayed = bool(
            project.target_commissioning_date
            and project.target_commissioning_date < date.today()
        )
        delay_days = (
            (date.today() - project.target_commissioning_date).days
            if is_delayed else None
        )
        current_phase  = (
            project.phases
            .filter(tasks__status__in=['Not Started', 'In Progress', 'Blocked'])
            .order_by('phase_order').first()
            or project.phases.order_by('-phase_order').first()
        )
        due_today_for_project = Task.objects.filter(
            phase__project=project,
            due_date=date.today(), due_date__isnull=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
        ).count()

        milestones_list = []
        for _m in project.milestones.all():
            milestones_list.append({
                'milestone_name':        _m.milestone_name,
                'milestone_description': _m.milestone_description,
                'amount':                _m.amount,
                'status':                _m.status,
            })

        projects_with_progress.append({
            'project':                   project,
            'total_tasks':               total_tasks,
            'done_tasks':                done_tasks,
            'internal_total':            internal_total,
            'internal_done':             internal_done,
            'internal_percent':          internal_percent,
            'external_pending':          ext_pending,
            'overdue_count':             overdue_count,
            'current_phase':             current_phase,
            'blocked_count':             blocked_count,
            'blocked_tasks_for_project': blocked_tasks_for_project,
            'overdue_tasks_for_project': overdue_tasks_for_project,
            'is_delayed':                is_delayed,
            'delay_days':                delay_days,
            'urgency_count':             blocked_count + overdue_count,
            'due_today_count':           due_today_for_project,
            'milestones':                milestones_list,
        })

    projects_with_progress.sort(
        key=lambda row: (row['overdue_count'] + row['blocked_count'], row['is_delayed']),
        reverse=True,
    )

    # Annotate material summary badges — one call to shared helper, not N+1 per project
    if projects_with_progress:
        pm_project_ids = [row['project'].project_id for row in projects_with_progress]
        delivery_lookup = _build_delivery_lookup(pm_project_ids)
        for row in projects_with_progress:
            row['material_summary'] = _project_material_badge(
                row['project'].project_id, delivery_lookup
            )

    # Delivery issues — one batch query grouped by project, attached to each row
    if projects_with_progress:
        _all_dc_issues = list(
            Issue.objects.filter(
                delivery_challan__project__assigned_pm=pm_profile,
                status__in=[Issue.OPEN, Issue.IN_PROGRESS],
            )
            .select_related('project', 'delivery_challan', 'raised_by__user')
            .order_by('-raised_at')
        )
        _dc_issues_by_project = {}
        for _issue in _all_dc_issues:
            _pid = _issue.project.project_id
            _dc_issues_by_project.setdefault(_pid, []).append(_issue)
        for row in projects_with_progress:
            row['delivery_issues'] = _dc_issues_by_project.get(row['project'].project_id, [])

    due_today_tasks = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        due_date=date.today(), due_date__isnull=False,
        task_type=Task.INTERNAL,
        status__in=['Not Started', 'In Progress'],
    ).select_related('phase__project', 'assigned_to').order_by('phase__project__project_id')

    blocked_tasks_list = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        status='Blocked',
    ).select_related('phase__project')

    pending_approvals_list = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        assigned_role=Task.PM,
        status='Not Started',
    ).select_related('phase__project')

    external_pending_list = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        task_type=Task.EXTERNAL,
        status__in=['Not Started', 'In Progress'],
    ).select_related('phase__project')

    team_due_today = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        due_date=date.today(), due_date__isnull=False,
        status__in=['Not Started', 'In Progress'],
    ).exclude(assigned_role=Task.PM).select_related('phase__project')

    seven_days_ago = date.today() - timedelta(days=7)
    due_date_changes = DueDateChangeLog.objects.filter(
        task__phase__project__assigned_pm=pm_profile,
        changed_at__date__gte=seven_days_ago,
    ).select_related('task__phase__project', 'changed_by__user').order_by('-changed_at')[:30]

    _pm_task_base = Task.objects.filter(
        phase__project__assigned_pm=pm_profile,
        phase__project__is_deleted=False,
        phase__project__status__in=['Active', 'In Progress'],
        due_date__isnull=False,
    )
    _active = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]
    _soon   = date.today() + timedelta(days=7)
    tasks_due_today_count = _pm_task_base.filter(due_date=date.today(), status__in=_active).count()
    tasks_due_soon_count  = _pm_task_base.filter(due_date__gt=date.today(), due_date__lte=_soon, status__in=_active).count()
    tasks_overdue_count   = _pm_task_base.filter(due_date__lt=date.today()).exclude(status=Task.DONE).count()

    return render(request, 'dashboard/pm.html', {
        'summary': {
            'active_projects':   active_projects,
            'due_today':         due_today,
            'blocked_tasks':     blocked_tasks,
            'pending_approvals': pending_approvals,
            'external_pending':  external_pending,
            'tasks_due_today':   tasks_due_today_count,
            'tasks_due_soon':    tasks_due_soon_count,
            'tasks_overdue':     tasks_overdue_count,
        },
        'draft_projects':          draft_projects,
        'projects_with_progress': projects_with_progress,
        'due_today_tasks':        due_today_tasks,
        'blocked_tasks_list':     blocked_tasks_list,
        'pending_approvals_list': pending_approvals_list,
        'external_pending_list':  external_pending_list,
        'team_due_today':         team_due_today,
        'due_date_changes':       due_date_changes,
        'today':                  date.today(),
        'all_profiles':           UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
@role_required(['Site Engineer'])
def dashboard_site_engineer(request):
    """SE dashboard. Renders one card per assigned project, sorted by urgency. Site Engineer role only."""
    #
    # Stop-and-report findings (verified against models.py 2026-06-19):
    # 1. SE-project link: Project.assigned_site_engineer FK → UserProfile (related_name='se_projects')
    # 2. DeliveryChallan.EXPECTED='Expected' = GRN not yet confirmed (no line items have received_quantity)
    # 3. is_delayed: view-computed (same logic as PM dashboard) via target_commissioning_date
    # 4. Blocked field: Task.status == 'Blocked' (string); no separate is_blocked boolean
    # 5. task_type values: Task.INTERNAL='Internal', Task.EXTERNAL='External'
    #
    # SE role only — other roles are redirected at the decorator level
    today      = date.today()
    se_profile = request.user.profile

    # Annotate each project with per-SE urgency counts in a single DB round-trip.
    # overdue_count: internal tasks assigned to this SE, past due, not done.
    # Excludes external/authority tasks (task_type='External') — DISCOM delays
    # must never corrupt internal execution metrics. See Day 2 architectural decision.
    # blocked_count: tasks assigned to SE with status='Blocked' (no is_blocked field — verified).
    # pending_grn_count: DCs for this project with status='Expected' (no GRN confirmed yet).
    # issue_count: open issues on this project (Open or In Progress).
    projects = Project.objects.filter(
        is_deleted=False,
        assigned_site_engineer=se_profile,
        status__in=['Active', 'In Progress'],
    ).annotate(
        overdue_count=Count(
            'phases__tasks',
            filter=Q(
                phases__tasks__task_type=Task.INTERNAL,
                phases__tasks__assigned_to=se_profile,
                phases__tasks__due_date__lt=today,
                phases__tasks__due_date__isnull=False,
                phases__tasks__status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
            ),
            distinct=True,
        ),
        blocked_count=Count(
            'phases__tasks',
            filter=Q(
                phases__tasks__assigned_to=se_profile,
                phases__tasks__status=Task.BLOCKED,
            ),
            distinct=True,
        ),
        pending_grn_count=Count(
            'delivery_challans',
            filter=Q(delivery_challans__status=DeliveryChallan.EXPECTED),
            distinct=True,
        ),
        issue_count=Count(
            'issues',
            filter=Q(issues__status__in=[Issue.OPEN, Issue.IN_PROGRESS]),
            distinct=True,
        ),
    )

    projects_data = []
    for project in projects:
        # Compute SE task progress: done / total for tasks explicitly assigned to this SE
        se_tasks_qs = Task.objects.filter(phase__project=project, assigned_to=se_profile)
        se_total    = se_tasks_qs.count()
        se_done     = se_tasks_qs.filter(status=Task.DONE).count()
        progress    = int(se_done / se_total * 100) if se_total else 0

        # is_delayed: same logic as PM dashboard — uses target_commissioning_date
        is_delayed = bool(
            project.target_commissioning_date
            and project.target_commissioning_date < today
        )
        delay_days = (today - project.target_commissioning_date).days if is_delayed else 0

        # Current phase: first phase with an incomplete task (same query pattern as PM dashboard)
        current_phase_obj = (
            project.phases
            .filter(tasks__status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])
            .order_by('phase_order').first()
            or project.phases.order_by('-phase_order').first()
        )
        phase_name = current_phase_obj.phase_name if current_phase_obj else None

        # Next task: earliest not-done SE-assigned task ordered by due_date.
        # PostgreSQL sorts NULLs last in ASC order, so tasks with dates appear first.
        next_task_obj = (
            Task.objects.filter(
                phase__project=project,
                assigned_to=se_profile,
                status__in=[Task.NOT_STARTED, Task.IN_PROGRESS],
            )
            .order_by('due_date')
            .first()
        )

        next_task    = None
        next_due     = None
        next_overdue = False
        if next_task_obj:
            next_task = next_task_obj.task_name
            if next_task_obj.due_date:
                # Use .day (no leading zero) + strftime('%b') — avoids %-d platform differences
                next_due     = '{} {}'.format(next_task_obj.due_date.day, next_task_obj.due_date.strftime('%b'))
                next_overdue = next_task_obj.due_date < today
            else:
                next_due = 'No due date set'

        urgency_count = (
            project.overdue_count
            + project.blocked_count
            + project.pending_grn_count
            + project.issue_count
        )

        projects_data.append({
            'project_id':        project.project_id,
            'pk':                project.pk,
            'customer_name':     project.customer_name,
            'phase':             phase_name,
            'progress':          progress,
            'is_delayed':        is_delayed,
            'delay_days':        delay_days,
            'overdue_count':     project.overdue_count,
            'blocked_count':     project.blocked_count,
            'pending_grn_count': project.pending_grn_count,
            'issue_count':       project.issue_count,
            'urgency_count':     urgency_count,
            'next_task':         next_task,
            'next_due':          next_due,
            'next_overdue':      next_overdue,
        })

    # Sort in Python after annotation — Django ORM cannot order by a computed
    # urgency_count (sum of multiple annotations) without a subquery.
    # With 3-5 projects per SE, a Python sort is negligible overhead.
    projects_data.sort(key=lambda p: p['urgency_count'], reverse=True)

    total_overdue     = sum(p['overdue_count'] for p in projects_data)
    total_pending_grn = sum(p['pending_grn_count'] for p in projects_data)
    total_issues      = sum(p['issue_count'] for p in projects_data)
    total_urgent      = sum(p['urgency_count'] for p in projects_data)

    _se_task_base = Task.objects.filter(
        phase__project__assigned_site_engineer=se_profile,
        phase__project__is_deleted=False,
        phase__project__status__in=['Active', 'In Progress'],
        due_date__isnull=False,
    )
    _se_active = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]
    _se_soon   = today + timedelta(days=7)
    se_tasks_due_today = _se_task_base.filter(due_date=today, status__in=_se_active).count()
    se_tasks_due_soon  = _se_task_base.filter(due_date__gt=today, due_date__lte=_se_soon, status__in=_se_active).count()
    se_tasks_overdue   = _se_task_base.filter(due_date__lt=today).exclude(status=Task.DONE).count()

    return render(request, 'dashboard/site-engineer.html', {
        'projects':          projects_data,
        'se_first_name':     request.user.first_name or request.user.username,
        'total_overdue':     total_overdue,
        'total_pending_grn': total_pending_grn,
        'total_issues':      total_issues,
        'total_urgent':      total_urgent,
        'tasks_due_today':   se_tasks_due_today,
        'tasks_due_soon':    se_tasks_due_soon,
        'tasks_overdue':     se_tasks_overdue,
        'today':             today,
        'all_profiles':      UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
@role_required(['Design'])
def dashboard_design(request):
    """Design dashboard. One card per assigned project, combined urgency circle. Design role only."""
    today          = date.today()
    design_profile = request.user.profile

    # Trigger 1: assigned_design FK confirmed on Project model.
    # Prefetch BOQ for each project — avoids N+1 when reading boq.status in loop.
    projects_qs = (
        Project.objects.filter(
            is_deleted=False,
            assigned_design=design_profile,
            status__in=['Active', 'In Progress'],
        )
        .prefetch_related('boq')
        .order_by('project_id')
    )

    # Summary totals — single query, scoped to this Design user's portfolio
    total_revisions = BOQ.objects.filter(
        project__assigned_design=design_profile,
        project__status__in=['Active', 'In Progress'],
        status='Revision Requested',
    ).count()

    # Trigger 5: no SLA/due-date mechanism exists for Design or BOQ submission.
    # total_design_overdue and total_boq_overdue hardcoded to 0 until Zuber
    # defines explicit thresholds in Claude Chat (same pattern as SCM aging thresholds).
    total_design_overdue = 0
    total_boq_overdue    = 0

    project_rows = []
    for project in projects_qs:
        # Trigger 4: is_delayed — same view-computed logic as PM/SE/Finance dashboards
        is_delayed = bool(
            project.target_commissioning_date
            and project.target_commissioning_date < today
        )
        delay_days = (
            (today - project.target_commissioning_date).days
            if is_delayed else None
        )

        # Trigger 3: BOQ status has 4 values — Draft / Submitted / Acknowledged /
        # Revision Requested. No BOQ at all is treated as Draft (not yet started).
        try:
            boq_status = project.boq.status  # Draft/Submitted/Acknowledged/Revision Requested
        except BOQ.DoesNotExist:
            boq_status = 'Draft'

        # Trigger 2: design_status derived from BOQ — 'submitted' when Design has
        # delivered a BOQ (Submitted or Acknowledged); 'pending' otherwise.
        design_status = 'submitted' if boq_status in ('Submitted', 'Acknowledged') else 'pending'

        # Trigger 4: revision_requested is a BOQ status value, not a separate model/flag.
        revision_requested = (boq_status == 'Revision Requested')

        # Trigger 5: overdue flags hardcoded False — no SLA/due-date mechanism exists
        # for Design or BOQ submission. Wire to real SLA logic once thresholds are
        # defined in Claude Chat. Status chips still display correctly without this.
        design_overdue = False  # TODO: wire to real SLA once thresholds defined
        boq_overdue    = False  # TODO: wire to real SLA once thresholds defined

        # Urgency formula: overdue items + explicit revision flag.
        # With both overdue flags False today, only revision_requested counts.
        urgency_count = (
            (1 if (design_status == 'pending' and design_overdue) else 0) +
            (1 if (boq_status not in ('Submitted', 'Acknowledged') and boq_overdue) else 0) +
            (1 if revision_requested else 0)
        )

        project_rows.append({
            'project':             project,
            'customer_name':       project.customer_name,
            'project_id':          project.project_id,
            'is_delayed':          is_delayed,
            'delay_days':          delay_days,
            'design_status':       design_status,
            'design_overdue':      design_overdue,
            'boq_status':          boq_status,
            'boq_overdue':         boq_overdue,
            'revision_requested':  revision_requested,
            'urgency_count':       urgency_count,
        })

    # Most-urgent-first; all-clear projects sink to bottom
    project_rows.sort(key=lambda r: r['urgency_count'], reverse=True)

    _design_task_base = Task.objects.filter(
        phase__project__assigned_design=design_profile,
        phase__project__is_deleted=False,
        phase__project__status__in=['Active', 'In Progress'],
        due_date__isnull=False,
    )
    _d_active = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]
    _d_soon   = today + timedelta(days=7)
    design_tasks_due_today = _design_task_base.filter(due_date=today, status__in=_d_active).count()
    design_tasks_due_soon  = _design_task_base.filter(due_date__gt=today, due_date__lte=_d_soon, status__in=_d_active).count()
    design_tasks_overdue   = _design_task_base.filter(due_date__lt=today).exclude(status=Task.DONE).count()

    return render(request, 'dashboard/design.html', {
        'design_first_name':    request.user.first_name,
        'total_revisions':      total_revisions,
        'total_design_overdue': total_design_overdue,
        'total_boq_overdue':    total_boq_overdue,
        'project_rows':         project_rows,
        'today':                today,
        'tasks_due_today':      design_tasks_due_today,
        'tasks_due_soon':       design_tasks_due_soon,
        'tasks_overdue':        design_tasks_overdue,
    })


@login_required
@role_required(['Finance'])
def dashboard_finance(request):
    """Finance dashboard. One card per project, milestone + payment-request status. Finance role only."""
    today = date.today()

    # Trigger 3: Finance is department-level — no assigned_finance field on Project.
    # Show all active/in-progress projects across the portfolio.
    # Trigger 1: milestones live on PaymentMilestone model (related_name='milestones').
    # Trigger 2: PaymentRequest exists — prefetch pending requests only, with vendor name.
    projects_qs = (
        Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
        .prefetch_related(
            'milestones',
            Prefetch(
                'payment_requests',
                queryset=PaymentRequest.objects.filter(
                    status=PaymentRequest.PENDING,
                ).select_related('vendor'),
                to_attr='pending_payment_requests',
            ),
        )
        .order_by('project_id')
    )

    # Top-level summary counts — single query each, scoped to active portfolio
    total_milestones_awaiting = PaymentMilestone.objects.filter(
        project__status__in=['Active', 'In Progress'],
        status=PaymentMilestone.PENDING,
    ).count()

    # Trigger 2: PaymentRequest confirmed present — query real pending count and value
    total_payment_requests = PaymentRequest.objects.filter(
        project__status__in=['Active', 'In Progress'],
        status=PaymentRequest.PENDING,
    ).count()

    total_payment_request_value = (
        PaymentRequest.objects.filter(
            project__status__in=['Active', 'In Progress'],
            status=PaymentRequest.PENDING,
        ).aggregate(s=Sum('amount'))['s'] or 0
    )

    total_client_contract_value = (
        Project.objects.filter(
            is_deleted=False,
            status__in=['Active', 'In Progress'],
        ).aggregate(s=Sum('contract_value'))['s'] or 0
    )

    project_rows = []
    for project in projects_qs:
        # Trigger 4: is_delayed — view-computed, same logic as PM/SE dashboards
        is_delayed = bool(
            project.target_commissioning_date
            and project.target_commissioning_date < today
        )
        delay_days = (
            (today - project.target_commissioning_date).days
            if is_delayed else None
        )

        # Trigger 1: build milestone list from PaymentMilestone rows
        milestones = []
        milestones_awaiting = 0
        for m in project.milestones.all():
            # Show the most recent relevant date: received > invoiced > due
            display_date = m.received_date or m.invoice_date or m.due_date
            milestones.append({
                'label':        m.milestone_name,
                'description':  m.milestone_description,
                'amount':       m.amount,
                'status':       m.status,
                'date':         display_date,
            })
            if m.status == PaymentMilestone.PENDING:
                milestones_awaiting += 1

        # Trigger 2: pending payment requests from prefetch (no extra query)
        payment_requests = []
        for pr in project.pending_payment_requests:
            payment_requests.append({
                'pk':             pr.pk,
                'vendor':         pr.vendor.name if pr.vendor else '—',
                'amount':         pr.amount,
                'invoice_number': pr.invoice_number,
                'requested_date': pr.requested_date,
            })

        # urgency drives card sort order: most attention needed rises to top
        urgency = milestones_awaiting + len(payment_requests)

        project_rows.append({
            'project':                project,
            'customer_name':          project.customer_name,
            'project_id':             project.project_id,
            'is_delayed':             is_delayed,
            'delay_days':             delay_days,
            'milestones':             milestones,
            'milestones_total':       len(milestones),
            'milestones_awaiting':    milestones_awaiting,
            'payment_requests':       payment_requests,
            'payment_requests_count': len(payment_requests),
            'urgency':                urgency,
        })

    # Most-urgent-first; all-clear projects sink to bottom
    project_rows.sort(key=lambda r: r['urgency'], reverse=True)

    return render(request, 'dashboard/finance.html', {
        'finance_first_name':           request.user.first_name,
        'total_milestones_awaiting':    total_milestones_awaiting,
        'total_payment_requests':       total_payment_requests,
        'total_payment_request_value':  total_payment_request_value,
        'total_client_contract_value':  total_client_contract_value,
        'project_rows':                 project_rows,
        'today':                        today,
    })


@login_required
@role_required(['SCM'])
def dashboard_scm(request):
    """
    SCM dashboard: BOQs awaiting acknowledgment, per-project DC status,
    pending deliveries, material badges, and delivery issues.
    All delivery/procurement signals read from DeliveryChallan/DCLineItem —
    not Task proxies — so they reflect actual receipt state. Access: SCM only.
    """
    today = date.today()

    # BOQ awaiting SCM acknowledgment (Q3 — confirmed working, left unchanged)
    boq_awaiting = BOQ.objects.filter(status='Submitted').count()

    # Deliveries today: DCs with expected_delivery_date=today not yet fully received
    deliveries_today = DeliveryChallan.objects.filter(
        project__status__in=['Active', 'In Progress'],
        expected_delivery_date=today,
        status__in=[DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED],
    ).count()

    # Overdue: DCs whose expected date has passed and are still unresolved (including Rejected = severe)
    overdue = DeliveryChallan.objects.filter(
        project__status__in=['Active', 'In Progress'],
        expected_delivery_date__lt=today,
        expected_delivery_date__isnull=False,
        status__in=[DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED, DeliveryChallan.REJECTED],
    ).count()

    # Active projects SCM tracks: all Active/In Progress non-deleted projects
    active_projects = list(
        Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
        .select_related('assigned_pm__user')
        .order_by('project_id')
    )
    active_project_ids = [p.project_id for p in active_projects]

    # dc_by_project: group all existing DCs by project for the Procurement section.
    # SCM uses this to see which projects have DCs and their current receipt state.
    # Projects with no DCs yet appear with an empty challan list.
    raw_challans = (
        DeliveryChallan.objects.filter(
            project__status__in=['Active', 'In Progress'],
        )
        .select_related('project', 'vendor')
        .order_by('project__project_id', '-dc_date')
    )
    dc_map = {p.project_id: {'project': p, 'challans': []} for p in active_projects}
    for challan in raw_challans:
        pid = challan.project.project_id
        if pid in dc_map:
            dc_map[pid]['challans'].append(challan)
    # Return as list of (project, challans) tuples — mirrors old pos_by_project structure
    # so the template loop shape stays the same (project header + per-challan rows)
    dc_by_project = [(row['project'], row['challans']) for row in dc_map.values()]

    # delivery_challans: unresolved DCs for the Deliveries section — includes Rejected (severe) at top
    # Rejected DCs need urgent SCM attention alongside Expected/Partial
    delivery_challans = (
        DeliveryChallan.objects.filter(
            project__status__in=['Active', 'In Progress'],
            status__in=[
                DeliveryChallan.EXPECTED,
                DeliveryChallan.PARTIALLY_RECEIVED,
                DeliveryChallan.REJECTED,
            ],
        )
        .select_related('project', 'vendor')
        .order_by('expected_delivery_date', 'project__project_id')
    )

    # Material badges — reuse shared helper; same aggregation as dashboard_pm, no drift risk
    delivery_lookup = _build_delivery_lookup(active_project_ids) if active_project_ids else {}

    # BOQ map: one query for all active projects (avoids N+1 in the loop below).
    # Key by b.project.project_id (string e.g. 'HRP-RES-2026-001'), NOT b.project_id
    # which is the integer auto-PK — mismatches the string pid used in the loop.
    boq_map = {
        b.project.project_id: b
        for b in BOQ.objects.filter(project__project_id__in=active_project_ids)
                             .select_related('project')
    }

    # DC grouping per project — reuse raw_challans (already fetched, ordered -dc_date per project)
    dc_per_project = {pid: row['challans'] for pid, row in dc_map.items()}

    # Open issue counts + oldest raised_at per project — one aggregation query
    issue_agg = (
        Issue.objects.filter(
            project__project_id__in=active_project_ids,
            status__in=[Issue.OPEN, Issue.IN_PROGRESS],
        )
        .values('project__project_id')
        .annotate(count=Count('pk'), oldest_at=Min('raised_at'))
    )
    issue_map = {}
    for _row in issue_agg:
        _pid = _row['project__project_id']
        _oldest = (_row['oldest_at'].date() if _row['oldest_at'] else None)
        issue_map[_pid] = {
            'count':      _row['count'],
            'oldest_age': (today - _oldest).days if _oldest else 0,
        }

    # Per-project pipeline rows with 4-stage status + stall computation
    project_rows = []
    for project in active_projects:
        pid = project.project_id
        boq          = boq_map.get(pid)
        challans     = dc_per_project.get(pid, [])
        material_bdg = _project_material_badge(pid, delivery_lookup)
        issue_data   = issue_map.get(pid, {'count': 0, 'oldest_age': None})

        # — BOQ stage —
        boq_status   = boq.status if boq else None
        boq_age_days = None
        if boq and boq.status == 'Submitted' and boq.submitted_at:
            boq_age_days = (today - boq.submitted_at.date()).days

        # — Order/Procurement stage —
        pending_challans = [c for c in challans if c.status in (
            DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED, DeliveryChallan.REJECTED
        )]
        if not challans:
            order_status   = 'None'
            order_age_days = None
        elif not pending_challans:
            order_status   = 'Received'
            order_age_days = None
        else:
            _severity_rank = {DeliveryChallan.REJECTED: 3,
                              DeliveryChallan.PARTIALLY_RECEIVED: 2,
                              DeliveryChallan.EXPECTED: 1}
            worst_pending  = max(pending_challans, key=lambda c: _severity_rank.get(c.status, 0))
            order_status   = ('Rejected' if worst_pending.status == DeliveryChallan.REJECTED else
                              'Partial'  if worst_pending.status == DeliveryChallan.PARTIALLY_RECEIVED else
                              'Expected')
            oldest_dc_date = min(c.dc_date for c in pending_challans)
            order_age_days = (today - oldest_dc_date).days

        # — Delivery stage —
        if material_bdg == 'Received':
            delivery_status   = 'Received'
            delivery_age_days = None
        else:
            overdue_challans = [
                c for c in challans
                if c.expected_delivery_date and c.expected_delivery_date < today
                and c.status != DeliveryChallan.RECEIVED
            ]
            unscheduled_challans = [
                c for c in challans
                if not c.expected_delivery_date and c.status != DeliveryChallan.RECEIVED
            ]
            if overdue_challans:
                delivery_status   = 'Overdue'
                delivery_age_days = max((today - c.expected_delivery_date).days
                                        for c in overdue_challans)
            elif unscheduled_challans:
                delivery_status   = 'Not Scheduled'
                oldest_dc         = min(c.dc_date for c in unscheduled_challans)
                delivery_age_days = (today - oldest_dc).days
            elif challans:
                delivery_status   = 'Scheduled'
                delivery_age_days = None
            else:
                delivery_status   = 'None'
                delivery_age_days = None

        # — Issues stage —
        open_issue_count = issue_data['count']
        issue_age_days   = issue_data['oldest_age']

        # — Stall computation — worst wins (red > amber; within same level, most days) —
        stall_candidates = []

        if boq_status == 'Submitted' and boq_age_days is not None:
            if boq_age_days > 5:
                stall_candidates.append(('boq', boq_age_days, 'red'))
            elif boq_age_days > 2:
                stall_candidates.append(('boq', boq_age_days, 'amber'))

        if order_status in ('Expected', 'Partial', 'Rejected') and order_age_days is not None:
            if order_age_days > 7:
                stall_candidates.append(('order', order_age_days, 'red'))
            elif order_age_days > 3:
                stall_candidates.append(('order', order_age_days, 'amber'))

        if delivery_status in ('Overdue', 'Not Scheduled') and delivery_age_days is not None:
            if delivery_age_days > 10:
                stall_candidates.append(('delivery', delivery_age_days, 'red'))
            elif delivery_age_days > 5:
                stall_candidates.append(('delivery', delivery_age_days, 'amber'))

        if open_issue_count > 0:
            _iage = issue_age_days or 0
            if _iage > 2:
                stall_candidates.append(('issues', _iage, 'red'))
            else:
                stall_candidates.append(('issues', _iage, 'amber'))

        if stall_candidates:
            _red = [(s, d, l) for (s, d, l) in stall_candidates if l == 'red']
            if _red:
                _worst     = max(_red, key=lambda x: x[1])
                stall_level = 'red'
            else:
                _worst     = max(stall_candidates, key=lambda x: x[1])
                stall_level = 'amber'
            is_stalled    = True
            stalled_stage = _worst[0]
            days_in_stage = _worst[1]
        else:
            is_stalled    = False
            stalled_stage = None
            stall_level   = None
            days_in_stage = None

        # — Action URLs —
        boq_url               = f'/projects/{pid}/boq/'
        schedule_delivery_url = f'/projects/{pid}/delivery-challans/create/'
        finance_url           = f'/projects/{pid}/overview/'
        # payment_request_url: POST endpoint for SCM to raise a payment request against a vendor invoice
        payment_request_url   = f'/projects/{pid}/payment-requests/raise/'
        # raise_issue_url: scope to the most recent pending DC if one exists
        latest_pending_dc = (pending_challans[0] if pending_challans
                             else (challans[0] if challans else None))
        if latest_pending_dc:
            raise_issue_url = (f'/projects/{pid}/delivery-challans/'
                               f'{latest_pending_dc.pk}/issues/create/')
        else:
            raise_issue_url = f'/projects/{pid}/issues/create/'

        project_rows.append({
            'project':             project,
            'material_badge':      material_bdg,
            'boq_status':          boq_status,
            'boq_age_days':        boq_age_days,
            'order_status':        order_status,
            'order_age_days':      order_age_days,
            'delivery_status':     delivery_status,
            'delivery_age_days':   delivery_age_days,
            'open_issue_count':    open_issue_count,
            'issue_age_days':      issue_age_days,
            'is_stalled':          is_stalled,
            'stalled_stage':       stalled_stage,
            'stall_level':         stall_level,
            'days_in_stage':       days_in_stage,
            'boq_url':             boq_url,
            'schedule_delivery_url': schedule_delivery_url,
            'finance_url':         finance_url,
            'payment_request_url': payment_request_url,
            'raise_issue_url':     raise_issue_url,
        })

    # Delivery issues: open/in-progress issues linked to DCs on active SCM-tracked projects
    # SCM scope: all active projects (SCM is not PM-scoped; it sees all active projects)
    delivery_issues = (
        Issue.objects.filter(
            delivery_challan__project__status__in=['Active', 'In Progress'],
            status__in=[Issue.OPEN, Issue.IN_PROGRESS],
        )
        .select_related('project', 'delivery_challan', 'raised_by__user')
        .order_by('-raised_at')[:20]
    )

    # BOQ items per project — grouped by category for the raise-payment-request dropdown.
    # Loaded here (not per-request in the modal) so the template can render them as JSON
    # once and the JS layer filters by project without extra round-trips.
    # BOQItem FK chain: BOQItem.boq → BOQ (OneToOne) → Project.
    all_boq_items = (
        BOQItem.objects.filter(boq__project__project_id__in=active_project_ids)
        .select_related('boq__project')
        .order_by('boq__project__project_id', 'serial_no')
    )
    boq_items_by_project = {}
    for item in all_boq_items:
        pid = item.boq.project.project_id
        cat = item.category
        boq_items_by_project.setdefault(pid, {}).setdefault(cat, []).append({
            'id':          item.pk,
            'serial_no':   item.serial_no,
            'description': item.description,
        })

    # All active vendors for the raise-payment-request vendor dropdown
    scm_vendors = list(
        Vendor.objects.filter(is_active=True).order_by('name')
        .values('id', 'name')
    )

    _scm_task_base = Task.objects.filter(
        phase__project__is_deleted=False,
        phase__project__status__in=['Active', 'In Progress'],
        due_date__isnull=False,
    )
    _s_active = [Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]
    _s_soon   = today + timedelta(days=7)
    scm_tasks_due_today = _scm_task_base.filter(due_date=today, status__in=_s_active).count()
    scm_tasks_due_soon  = _scm_task_base.filter(due_date__gt=today, due_date__lte=_s_soon, status__in=_s_active).count()
    scm_tasks_overdue   = _scm_task_base.filter(due_date__lt=today).exclude(status=Task.DONE).count()

    return render(request, 'dashboard/scm.html', {
        'summary': {
            'boq_awaiting':     boq_awaiting,
            'deliveries_today': deliveries_today,
            'overdue':          overdue,
            'tasks_due_today':  scm_tasks_due_today,
            'tasks_due_soon':   scm_tasks_due_soon,
            'tasks_overdue':    scm_tasks_overdue,
        },
        'project_rows':         project_rows,
        'delivery_issues':      delivery_issues,
        'today':                today,
        'all_profiles':         UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
        'boq_items_by_project': json.dumps(boq_items_by_project),
        'scm_vendors':          json.dumps(scm_vendors),
    })


def _get_ceo_dashboard_context():
    """
    Aggregates portfolio-wide metrics for the CEO dashboard in exactly 3 DB queries.
    Query 1: Active project list with Exists annotations for blocked/at_risk classification.
    Query 2: Full task aggregate — status counts, dept rollup (18 cells), KPI time windows,
             blocked KPI, external-dependency KPI — all via a single .aggregate() call.
    Query 3: Full issue aggregate — status counts and resolution time windows.
    Returns a dict ready to be unpacked into the template context.
    """
    today  = date.today()
    now_dt = timezone.now()

    # -- Date-window boundaries (computed once; reused across all conditional filters) --
    week_start       = today - timedelta(days=today.weekday())           # Monday of current ISO week
    week_end         = week_start + timedelta(days=7)                    # Monday of next week (exclusive)
    last_week_start  = week_start - timedelta(days=7)
    last_week_end    = week_start                                        # == this week's Monday
    this_month_start = today.replace(day=1)
    if today.month == 12:
        next_month_start = date(today.year + 1, 1, 1)
    else:
        next_month_start = date(today.year, today.month + 1, 1)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

    # completed_at / resolved_at are DateTimeFields — boundaries must be datetimes
    from django.utils.timezone import make_aware
    def _to_dt(d):
        return make_aware(datetime(d.year, d.month, d.day, 0, 0, 0))

    week_start_dt       = _to_dt(week_start)
    week_end_dt         = _to_dt(week_end)
    last_week_start_dt  = _to_dt(last_week_start)
    last_week_end_dt    = week_start_dt
    this_month_start_dt = _to_dt(this_month_start)
    next_month_start_dt = _to_dt(next_month_start)
    last_month_start_dt = _to_dt(last_month_start)
    aged_block_cutoff   = now_dt - timedelta(days=7)

    active_statuses = ['Active', 'In Progress']

    # -- QUERY 1: Active project list + Exists subquery annotations --
    # Subquery: does this project have any task with status='Blocked'?
    blocked_subq = Task.objects.filter(
        phase__project=OuterRef('pk'),
        status=Task.BLOCKED,
    )
    # Subquery: does this project have any overdue internal task (still future target date handled in Python)?
    at_risk_subq = Task.objects.filter(
        phase__project=OuterRef('pk'),
        task_type=Task.INTERNAL,
        due_date__lt=today,
        due_date__isnull=False,
        status__in=[Task.NOT_STARTED, Task.IN_PROGRESS],
    )
    projects_qs = (
        Project.objects
        .filter(status__in=active_statuses)
        .annotate(
            has_blocked_task=Exists(blocked_subq),
            has_at_risk_task=Exists(at_risk_subq),
        )
        .order_by('target_commissioning_date', 'project_id')
    )

    # Classify each project in Python (no extra DB hits)
    # Precedence: Blocked > Delayed > At Risk > On Time (worst condition wins)
    proj_total = proj_on_time = proj_at_risk = proj_delayed = proj_blocked = 0
    project_cards = []
    for p in projects_qs:
        proj_total += 1
        is_delayed = bool(p.target_commissioning_date and p.target_commissioning_date < today)

        if p.has_blocked_task:
            badge = 'blocked'
            proj_blocked += 1
        elif is_delayed:
            badge = 'delayed'
            proj_delayed += 1
        elif p.has_at_risk_task and not is_delayed:
            # At Risk = overdue internal task but target date still in the future
            badge = 'at_risk'
            proj_at_risk += 1
        else:
            badge = 'on_time'
            proj_on_time += 1

        project_cards.append({'project': p, 'badge': badge, 'is_delayed': is_delayed})

    # -- QUERY 2: Task aggregate — single .aggregate() call, ~40 conditional Counts --
    task_agg = Task.objects.filter(
        phase__project__status__in=active_statuses,
    ).aggregate(
        task_total     =Count('pk'),
        # Status summary (portfolio-wide)
        task_unassigned=Count('pk', filter=Q(assigned_to__isnull=True)),
        task_inprogress=Count('pk', filter=Q(status=Task.IN_PROGRESS)),
        task_completed =Count('pk', filter=Q(status=Task.DONE)),
        # Overdue = internal tasks only; external delays are not team overdue (SE has DISCOM tasks)
        task_overdue   =Count('pk', filter=Q(
            task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS],
        )),
        # Blocked KPI (two distinct numbers — see Layer 5 §3: they will not reconcile, that is expected)
        blocked_open   =Count('pk', filter=Q(status=Task.BLOCKED)),
        blocked_aged_7d=Count('pk', filter=Q(
            status=Task.BLOCKED,
            blocked_since__lte=aged_block_cutoff,
            blocked_since__isnull=False,
        )),
        # External dependency KPI
        ext_closed =Count('pk', filter=Q(task_type=Task.EXTERNAL, status=Task.DONE)),
        ext_overdue=Count('pk', filter=Q(
            task_type=Task.EXTERNAL, due_date__lt=today, due_date__isnull=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
        )),
        # KPI time windows — use completed_at (DateTimeField, set by task_status_update on Done)
        task_done_this_week  =Count('pk', filter=Q(status=Task.DONE, completed_at__gte=week_start_dt,       completed_at__lt=week_end_dt)),
        task_done_last_week  =Count('pk', filter=Q(status=Task.DONE, completed_at__gte=last_week_start_dt,  completed_at__lt=last_week_end_dt)),
        task_done_this_month =Count('pk', filter=Q(status=Task.DONE, completed_at__gte=this_month_start_dt, completed_at__lt=next_month_start_dt)),
        task_done_last_month =Count('pk', filter=Q(status=Task.DONE, completed_at__gte=last_month_start_dt, completed_at__lt=this_month_start_dt)),
        # -- Department rollup: 6 roles × 3 columns = 18 conditional Counts --
        # PM
        dept_pm_assigned=Count('pk', filter=Q(assigned_role=Task.PM, assigned_to__isnull=False)),
        dept_pm_pending =Count('pk', filter=Q(assigned_role=Task.PM, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_pm_overdue =Count('pk', filter=Q(assigned_role=Task.PM, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
        # Site Engineer — overdue MUST filter Internal; SE tasks include DISCOM/authority External tasks
        dept_se_assigned=Count('pk', filter=Q(assigned_role=Task.SITE_ENGINEER, assigned_to__isnull=False)),
        dept_se_pending =Count('pk', filter=Q(assigned_role=Task.SITE_ENGINEER, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_se_overdue =Count('pk', filter=Q(assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
        # SCM
        dept_scm_assigned=Count('pk', filter=Q(assigned_role=Task.SCM, assigned_to__isnull=False)),
        dept_scm_pending =Count('pk', filter=Q(assigned_role=Task.SCM, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_scm_overdue =Count('pk', filter=Q(assigned_role=Task.SCM, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
        # Design
        dept_design_assigned=Count('pk', filter=Q(assigned_role=Task.DESIGN, assigned_to__isnull=False)),
        dept_design_pending =Count('pk', filter=Q(assigned_role=Task.DESIGN, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_design_overdue =Count('pk', filter=Q(assigned_role=Task.DESIGN, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
        # BD / Sales — filter on assigned_role='BD / Sales' (Task.BD constant); 'BD' alone returns zero
        dept_bd_assigned=Count('pk', filter=Q(assigned_role=Task.BD, assigned_to__isnull=False)),
        dept_bd_pending =Count('pk', filter=Q(assigned_role=Task.BD, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_bd_overdue =Count('pk', filter=Q(assigned_role=Task.BD, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
        # Finance — overdue will read near-zero if finance tasks lack due dates; that is expected, not a bug
        dept_finance_assigned=Count('pk', filter=Q(assigned_role=Task.FINANCE, assigned_to__isnull=False)),
        dept_finance_pending =Count('pk', filter=Q(assigned_role=Task.FINANCE, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED])),
        dept_finance_overdue =Count('pk', filter=Q(assigned_role=Task.FINANCE, task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False, status__in=[Task.NOT_STARTED, Task.IN_PROGRESS])),
    )

    # -- QUERY 3: Issue aggregate — status counts + resolution time windows --
    issue_agg = Issue.objects.filter(
        project__status__in=active_statuses,
    ).aggregate(
        issue_total     =Count('pk'),
        issue_unassigned=Count('pk', filter=Q(assigned_to__isnull=True)),
        issue_inprogress=Count('pk', filter=Q(status=Issue.IN_PROGRESS)),
        # Include Closed: terminal state after Resolved; PM closes issues from the Resolved state
        issue_resolved  =Count('pk', filter=Q(status__in=[Issue.RESOLVED, Issue.CLOSED])),
        # Overdue uses due_date (nullable); will read low if few issues have due dates — accepted limitation
        issue_overdue   =Count('pk', filter=Q(
            due_date__lt=today, due_date__isnull=False,
            status__in=[Issue.OPEN, Issue.IN_PROGRESS],
        )),
        # Time windows use resolved_at; Closed issues that went through Resolved will also have it set
        issue_done_this_week  =Count('pk', filter=Q(resolved_at__isnull=False, resolved_at__gte=week_start_dt,       resolved_at__lt=week_end_dt)),
        issue_done_last_week  =Count('pk', filter=Q(resolved_at__isnull=False, resolved_at__gte=last_week_start_dt,  resolved_at__lt=last_week_end_dt)),
        issue_done_this_month =Count('pk', filter=Q(resolved_at__isnull=False, resolved_at__gte=this_month_start_dt, resolved_at__lt=next_month_start_dt)),
        issue_done_last_month =Count('pk', filter=Q(resolved_at__isnull=False, resolved_at__gte=last_month_start_dt, resolved_at__lt=this_month_start_dt)),
    )

    # Build dept_rows list for clean template iteration
    dept_rows = [
        {'label': 'PM',        'assigned': task_agg['dept_pm_assigned'],     'pending': task_agg['dept_pm_pending'],     'overdue': task_agg['dept_pm_overdue']},
        {'label': 'SCM',       'assigned': task_agg['dept_scm_assigned'],    'pending': task_agg['dept_scm_pending'],    'overdue': task_agg['dept_scm_overdue']},
        {'label': 'Design',    'assigned': task_agg['dept_design_assigned'], 'pending': task_agg['dept_design_pending'], 'overdue': task_agg['dept_design_overdue']},
        {'label': 'BD',        'assigned': task_agg['dept_bd_assigned'],     'pending': task_agg['dept_bd_pending'],     'overdue': task_agg['dept_bd_overdue']},
        {'label': 'Execution', 'assigned': task_agg['dept_se_assigned'],     'pending': task_agg['dept_se_pending'],     'overdue': task_agg['dept_se_overdue']},
        {'label': 'Finance',   'assigned': task_agg['dept_finance_assigned'],'pending': task_agg['dept_finance_pending'],'overdue': task_agg['dept_finance_overdue']},
    ]

    ctx = {
        'proj_total':    proj_total,
        'proj_on_time':  proj_on_time,
        'proj_at_risk':  proj_at_risk,
        'proj_delayed':  proj_delayed,
        'proj_blocked':  proj_blocked,
        'project_cards': project_cards,
        'dept_rows':     dept_rows,
    }
    ctx.update(task_agg)
    ctx.update(issue_agg)
    return ctx


@login_required
def dashboard_ceo(request):
    """CEO portfolio overview. Access: CEO role only. Renders in 3 DB queries via _get_ceo_dashboard_context."""
    ctx = _get_ceo_dashboard_context()
    ctx['now'] = timezone.now()
    return render(request, 'dashboard/ceo.html', ctx)


# ---------------------------------------------------------------------------
# User management (Admin only)
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def user_list(request):
    """List all user profiles with their roles. Access: Admin only."""
    profiles = UserProfile.objects.select_related('user', 'created_by').order_by('user__username')
    return render(request, 'users/user_list.html', {'profiles': profiles})


@login_required
@role_required(['Admin'])
def user_create(request):
    """
    Create a new Django User + UserProfile in one step.
    Sets is_staff=True when role is Admin so Django admin access is granted automatically.
    Access: Admin only.
    """
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            user = User.objects.create_user(
                username=cd['username'],
                password=cd['password'],
                first_name=cd['first_name'],
                last_name=cd['last_name'],
                email=cd['email'],
                is_active=cd['is_active'],
                is_staff=(cd['role'] == 'Admin'),
            )
            # UserProfile is auto-created by a post_save signal on User creation
            profile = user.profile
            profile.role = cd['role']
            profile.phone_number = cd['phone_number']
            profile.is_active = cd['is_active']
            profile.created_by = request.user
            profile.save()

            messages.success(
                request,
                f"User {cd['username']} created successfully as {cd['role']}"
            )
            return redirect('user_list')
    else:
        form = UserCreateForm()

    return render(request, 'users/user_form.html', {'form': form, 'action': 'Create'})


@login_required
@role_required(['Admin'])
def user_edit(request, user_id):
    """
    Edit an existing user's name, email, role, and active status.
    Uses filter().update() on UserProfile to avoid a second .save() round-trip.
    Access: Admin only.
    """
    target_user = get_object_or_404(User, pk=user_id)
    try:
        profile = target_user.profile
    except UserProfile.DoesNotExist:
        # Safety net: create a blank profile if one somehow doesn't exist
        profile = UserProfile.objects.create(user=target_user)

    if request.method == 'POST':
        form = UserEditForm(request.POST, instance_user=target_user)
        if form.is_valid():
            cd = form.cleaned_data
            target_user.first_name = cd['first_name']
            target_user.last_name = cd['last_name']
            target_user.email = cd['email']
            target_user.is_active = cd['is_active']
            target_user.is_staff = (cd['role'] == 'Admin')
            target_user.save()

            UserProfile.objects.filter(user=target_user).update(
                role=cd['role'],
                phone_number=cd['phone_number'],
                is_active=cd['is_active'],
            )

            messages.success(request, f"User {target_user.username} updated successfully.")
            return redirect('user_list')
    else:
        form = UserEditForm(
            initial={
                'first_name':   target_user.first_name,
                'last_name':    target_user.last_name,
                'email':        target_user.email,
                'role':         profile.role,
                'phone_number': profile.phone_number,
                'is_active':    profile.is_active,
            },
            instance_user=target_user,
        )

    return render(request, 'users/user_form.html', {
        'form': form,
        'action': 'Edit',
        'target_user': target_user,
    })


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _get_user_role(request):
    """Return the role string for the request user, or None if no profile exists."""
    try:
        return request.user.profile.role
    except Exception:
        return None


def _pm_owns_project(request, project):
    """Return True if the request user is the assigned PM on this project."""
    return project.assigned_pm is not None and project.assigned_pm.user == request.user


@login_required
@role_required(['PM', 'Admin', 'CEO'])
def project_list(request):
    """
    Redirect to the role-appropriate projects view.
    Admin  → Admin Panel project list (full table with assign-PM + delete)
    PM     → PM dashboard (draft + active project cards)
    CEO    → CEO dashboard
    Kept as a named URL so '← Projects' links in project_overview still resolve.
    """
    role = _get_user_role(request)
    if role == 'Admin':
        return redirect('admin_project_list')
    if role == 'CEO':
        return redirect('dashboard_ceo')
    return redirect('dashboard_pm')


@login_required
@role_required(['PM'])
def project_create(request):
    """
    Create a new Draft project. The PM is auto-set to the current user.
    project_id is generated inside Project.save() via generate_project_id().
    Access: PM only.
    """
    if request.method == 'POST':
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                project = form.save(commit=False)
                project.assigned_pm = request.user.profile
                project.status = 'Draft'
                project.created_by = request.user
                project.save()

            messages.success(request, f"Project {project.project_id} created successfully.")
            return redirect('project_overview', project_id=project.project_id)
    else:
        form = ProjectCreateForm(initial={'project_type': 'Residential'})

    return render(request, 'projects/project_form.html', {
        'form': form,
        'action': 'Create',
        'assigned_pm_display': request.user.get_full_name() or request.user.username,
    })


@login_required
def project_detail(request, project_id):
    """Merged into project_overview — redirect all traffic there."""
    return redirect('project_overview', project_id=project_id)


@login_required
@role_required(['Admin'])
def project_delete(request, project_id):
    """Soft-delete a project. Admin only, POST only."""
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)
    project = get_object_or_404(Project, project_id=project_id, is_deleted=False)
    project.is_deleted = True
    project.deleted_at = timezone.now()
    project.save(update_fields=['is_deleted', 'deleted_at'])
    messages.success(request, f"Project {project.project_id} ({project.customer_name}) has been deleted.")
    return redirect('project_list')


@login_required
@role_required(['PM'])
def project_edit(request, project_id):
    """
    Edit a Draft project's fields. Active+ projects are locked — edit is blocked
    with a warning redirect. Access: assigned PM only, Draft status only.
    """
    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    if project.status != 'Draft':
        messages.warning(request, 'Active projects cannot be edited.')
        return redirect('project_overview', project_id=project.project_id)

    if request.method == 'POST':
        form = ProjectEditForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            messages.success(request, f"Project {project.project_id} updated successfully.")
            return redirect('project_overview', project_id=project.project_id)
    else:
        form = ProjectEditForm(instance=project)

    return render(request, 'projects/project_form.html', {
        'form':               form,
        'action':             'Edit',
        'project':            project,
        'assigned_pm_display': project.assigned_pm.user.get_full_name() or project.assigned_pm.user.username,
    })


@login_required
@role_required(['PM'])
def project_activate(request, project_id):
    """
    Activate a Draft project: sets status=Active, stamps activated_at,
    attaches the Residential template (phases + tasks), and creates M1/M2/M3 milestones.
    All DB writes are wrapped in transaction.atomic() — a failure rolls back everything.
    Access: assigned PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    # Status check before any DB write — avoids partial state if already active
    if project.status != 'Draft':
        messages.warning(request, 'Project is already active.')
        return redirect('project_overview', project_id=project.project_id)

    with transaction.atomic():
        project.status = 'Active'
        project.activated_at = timezone.now()
        project.save()

        if project.project_type == 'Residential':
            attach_residential_template(project)

        milestone_defaults = [
            ('M1', 'On Survey Completion'),
            ('M2', 'On Material Supply'),
            ('M3', 'On Commissioning'),
        ]
        for name, desc in milestone_defaults:
            PaymentMilestone.objects.create(
                project=project,
                milestone_name=name,
                milestone_description=desc,
                created_by=request.user.profile,
            )

    # Log project activation after the transaction commits
    log_activity(project, request.user.profile, f"Activated project: {project.project_id}", entity_type='Project', entity_id=project.pk)

    if project.project_type == 'Residential':
        messages.success(request, 'Project activated. 50 tasks created. Set the first task due date to calculate all dates.')
    else:
        messages.success(request, 'Project activated. Add tasks manually using Add Task.')

    return redirect('project_overview', project_id=project.project_id)


@login_required
@role_required(['PM'])
def project_recalculate_dates(request, project_id):
    """
    Recalculate all task due dates from project.activated_at using the duration_days chain.
    Intended as a bulk reset; PM normally sets dates task-by-task via task_set_due_date.
    Access: assigned PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    if project.status == 'Draft':
        messages.warning(request, 'Project must be activated before calculating due dates.')
        return redirect('project_overview', project_id=project.project_id)

    if not project.activated_at:
        messages.warning(request, 'Project has no activation date — cannot calculate due dates.')
        return redirect('project_overview', project_id=project.project_id)

    calculate_due_dates(project)
    messages.success(request, 'Due dates recalculated from activation date.')
    return redirect('project_overview', project_id=project.project_id)


@login_required
def enable_cascade_scheduling(request, project_id):
    """
    Irreversibly enable cascading scheduling for a project.
    POST only, PM only, feature gate must be ON.
    Once set to True, cascade_scheduling cannot be reverted.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise PermissionDenied

    settings_obj = SystemSettings.get()
    if not settings_obj.cascade_scheduling_enabled:
        messages.error(request, 'Cascading scheduling is not enabled by Admin.')
        return redirect('project_overview', project_id=project_id)

    # Idempotent — already on, nothing to do
    if project.cascade_scheduling:
        return redirect('project_overview', project_id=project_id)

    project.cascade_scheduling = True
    project.save(update_fields=['cascade_scheduling'])

    # Trigger full recalculation if project has an activation date
    if project.activated_at:
        calculate_due_dates(project)

    log_activity(
        project=project,
        actor=request.user.profile,
        action=f"Cascading scheduling enabled for project '{project.project_id}'",
        entity_type='Project',
        entity_id=project.id,
    )

    messages.success(request, 'Cascading scheduling is now active for this project.')
    return redirect('project_overview', project_id=project.project_id)


@login_required
@role_required(['PM'])
def task_add(request, project_id):
    """
    Add a single manual task to an active project. Only allowed when status=Active.
    task_order is set to last+1 within the chosen phase.
    Access: assigned PM only.
    """
    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    if project.status != 'Active':
        messages.warning(request, 'Tasks can only be added to active projects.')
        return redirect('project_overview', project_id=project.project_id)

    if request.method == 'POST':
        form = TaskAddForm(request.POST, project=project)
        if form.is_valid():
            cd = form.cleaned_data
            phase = cd['phase']
            last_order = phase.tasks.aggregate(Max('task_order'))['task_order__max'] or 0
            Task.objects.create(
                phase=phase,
                task_name=cd['task_name'],
                task_order=last_order + 1,
                assigned_role=cd['assigned_role'],
                due_date=cd['due_date'],
            )
            messages.success(request, f"Task '{cd['task_name']}' added successfully.")
            return redirect('project_overview', project_id=project.project_id)
    else:
        form = TaskAddForm(project=project)

    return render(request, 'projects/task_add_form.html', {
        'form':    form,
        'project': project,
    })


@login_required
def task_status_update(request, project_id, task_id):
    """
    Update a task's status. Enforces a transition table so invalid moves are rejected.
    The assigned role or the project PM may update. Blocking a task requires a title
    for a new Issue — the issue is auto-created and linked to the task.
    filter().update() used instead of .save() to prevent race condition on concurrent
    status changes from two users.
    Access: task's assigned_role or project PM. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    if task.assigned_to is None:
        return JsonResponse({
            'success': False,
            'error': 'Task is unassigned. Assign it before changing status.'
        }, status=400)

    # Permission check before any DB write
    try:
        user_role = request.user.profile.role
    except Exception:
        user_role = None

    is_pm = _pm_owns_project(request, project)

    # Task.BD = 'BD / Sales' but UserProfile stores 'BD' — normalise before comparison
    _PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}
    normalised_user_role = _PROFILE_TO_TASK_ROLE.get(user_role, user_role)

    if normalised_user_role != task.assigned_role and not is_pm:
        return HttpResponseForbidden()

    new_status = request.POST.get('status', '').strip()
    valid_statuses = {s[0] for s in Task.STATUS_CHOICES}
    if new_status not in valid_statuses:
        messages.error(request, 'Invalid status value.')
        return redirect('project_overview', project_id=project.project_id)

    # State machine: defines allowed next states for each current state.
    # DONE can only go to BLOCKED (not back to In Progress) — prevents gaming completion.
    VALID_TRANSITIONS = {
        Task.NOT_STARTED: {Task.IN_PROGRESS, Task.BLOCKED, Task.DONE},
        Task.IN_PROGRESS: {Task.DONE, Task.BLOCKED},
        Task.BLOCKED:     {Task.IN_PROGRESS, Task.BLOCKED},
        Task.DONE:        {Task.BLOCKED},
    }

    allowed = VALID_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        messages.error(
            request,
            f"Cannot move task from '{task.status}' to '{new_status}'."
        )
        return redirect('project_overview', project_id=project.project_id)

    # Finance can supply due_date inline when switching to In Progress — save before the guard
    _due_date_str = request.POST.get('due_date', '').strip()
    if _due_date_str and new_status == Task.IN_PROGRESS and not task.due_date:
        try:
            _parsed_due = date.fromisoformat(_due_date_str)
            Task.objects.filter(pk=task.pk).update(due_date=_parsed_due)
            task.due_date = _parsed_due
        except ValueError:
            pass

    # Server-side guard: In Progress requires a due date
    if new_status == Task.IN_PROGRESS and not task.due_date:
        messages.warning(request, 'Please set a due date before marking this task as In Progress.')
        return redirect('project_overview', project_id=project.project_id)

    update_kwargs = {'status': new_status}
    if new_status == Task.DONE:
        update_kwargs['completed_at'] = timezone.now()
    # Track when a task first becomes blocked so the CEO aged-KPI can measure how long it has been stuck
    if new_status == Task.BLOCKED and task.status != Task.BLOCKED:
        update_kwargs['blocked_since'] = timezone.now()
    # Clear on un-block so any future re-block re-ages from zero, not from the original block date
    elif new_status != Task.BLOCKED and task.status == Task.BLOCKED:
        update_kwargs['blocked_since'] = None

    # Blocked requires a stated blocking issue (fresh transition only)
    if new_status == Task.BLOCKED and task.status != Task.BLOCKED:
        block_issue_title = request.POST.get('block_issue_title', '').strip()
        if not block_issue_title:
            messages.error(request, 'Please state the blocking issue before marking this task as Blocked.')
            next_url = request.POST.get('next', '')
            if next_url and not _urlparse(next_url).netloc:
                return redirect(next_url)
            return redirect('project_overview', project_id=project.project_id)

        Task.objects.filter(pk=task.pk).update(**update_kwargs)

        block_severity = request.POST.get('block_issue_severity', Issue.HIGH)
        if block_severity not in dict(Issue.SEVERITY_CHOICES):
            block_severity = Issue.HIGH
        block_assignee = None
        block_assignee_id = request.POST.get('block_issue_assigned_to', '').strip()
        if block_assignee_id:
            try:
                block_assignee = UserProfile.objects.get(pk=block_assignee_id)
            except UserProfile.DoesNotExist:
                pass
        issue = Issue.objects.create(
            project=project,
            task=task,
            title=block_issue_title,
            description=request.POST.get('block_issue_description', '').strip(),
            severity=block_severity,
            status=Issue.OPEN,
            raised_by=request.user.profile,
            assigned_to=block_assignee,
        )
        log_activity(
            project, request.user.profile,
            f"Blocked task '{task.task_name}' — issue: {block_issue_title}",
            entity_type='Issue', entity_id=issue.pk,
        )
        messages.success(request, f'Task blocked. Issue "{block_issue_title}" created.')
    else:
        Task.objects.filter(pk=task.pk).update(**update_kwargs)
        # Log status changes for all non-blocked transitions (blocked has its own log above)
        log_activity(project, request.user.profile, f"Changed task status to {new_status}: {task.task_name}", entity_type='Task', entity_id=task.pk)

        # Bidirectional sync: Finance confirmation tasks → PaymentMilestone Received.
        # Mapping by task name — names are fixed in the residential template.
        _FINANCE_TASK_TO_MILESTONE = {
            'Advance Payment Confirmation': 'M1',
            'Finance Confirmation':         'M2',
            '100% Payment Confirmation':    'M3',
        }
        if new_status == Task.DONE and task.task_name in _FINANCE_TASK_TO_MILESTONE:
            _ms_label  = _FINANCE_TASK_TO_MILESTONE[task.task_name]
            _ms_update = {'status': 'Received', 'received_date': date.today()}
            _ar_str    = request.POST.get('amount_received', '').strip()
            _vr_str    = request.POST.get('variance_reason', '').strip()
            if _ar_str:
                try:
                    _ms_update['amount_received'] = Decimal(_ar_str)
                except InvalidOperation:
                    pass
            if _vr_str:
                _ms_update['variance_reason'] = _vr_str
            try:
                PaymentMilestone.objects.filter(
                    project=project,
                    milestone_name=_ms_label,
                    status__in=['Pending', 'Invoiced'],
                ).update(**_ms_update)
            except Exception:
                pass  # Non-critical — never block the task update

        # Payment milestone notification: task.is_payment_milestone is read from the pre-update
        # object — the flag never changes during a status update, so this is safe.
        if new_status == Task.DONE and task.is_payment_milestone:
            seen_pks = set()
            milestone_recipients = list(UserProfile.objects.filter(role='Finance', is_active=True))
            if project.assigned_pm:
                milestone_recipients.append(project.assigned_pm)
            milestone_recipients += list(UserProfile.objects.filter(role__in=['BD', 'CEO'], is_active=True))
            for recipient in milestone_recipients:
                if recipient.pk in seen_pks:
                    continue
                seen_pks.add(recipient.pk)
                _pm_link = f'/projects/{project.project_id}/'
                _pm_message = (
                    f'Project {project.project_id} ({project.customer_name}) has reached '
                    f'payment milestone: "{task.task_name}".\n\n'
                    f'Please initiate collection at the earliest.'
                )
                _pm_email_message = (
                    f'{_pm_message}\n\nView in Horizon Solar PMS:\n'
                    f'https://horizon-solar-pms-production.up.railway.app{_pm_link}'
                )
                send_notification(
                    recipient=recipient,
                    message=_pm_email_message,
                    channels=['in_app', 'whatsapp', 'email'],
                    link=_pm_link,
                    subject=f'Payment Milestone Reached — {project.customer_name}',
                    template='payment_notification',
                    template_params=[project.customer_name, task.task_name, project.customer_name],
                    related_project=project,
                    actor=request.user.profile,
                )

    # Honour the ?next= redirect if it's a local URL (netloc empty = same domain)
    next_url = request.POST.get('next', None)
    if next_url:
        from urllib.parse import urlparse
        if urlparse(next_url).netloc == '':
            return redirect(next_url)
    return redirect('project_overview', project_id=project.project_id)


@login_required
@role_required(['PM'])
def task_assign(request, project_id, task_id):
    """
    Assign (or clear) the individual user on a task. Candidate list is filtered
    to the task's assigned_role so a PM-role task can only be given to a PM user.
    filter().update() used to avoid overwriting other task fields via .save().
    Access: assigned PM only.
    """
    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    # Task.ROLE_CHOICES uses 'BD / Sales' but UserProfile stores 'BD'
    _TASK_TO_PROFILE_ROLE = {'BD / Sales': 'BD'}
    profile_role = _TASK_TO_PROFILE_ROLE.get(task.assigned_role, task.assigned_role)

    # Candidates scoped to the task's role — prevents assigning a Finance user to a PM task
    candidates = UserProfile.objects.filter(role=profile_role, is_active=True)

    if request.method == 'POST':
        assigned_to_id = request.POST.get('assigned_to', '').strip()
        if assigned_to_id:
            assignee = get_object_or_404(UserProfile, pk=assigned_to_id, role=profile_role, is_active=True)
            Task.objects.filter(pk=task.pk).update(assigned_to=assignee)
            recipient_name = assignee.user.get_full_name() or assignee.user.username
            task_url = f'/projects/{project.project_id}/tasks/{task.pk}/'
            task_url_abs = request.build_absolute_uri(task_url)
            _at_message = (
                f'Hi {recipient_name},\n\n'
                f'The task "{task.task_name}" on project {project.customer_name} has been assigned to you.\n\n'
                f'Please login to review details and update progress.'
            )
            _at_email_message = (
                f'{_at_message}\n\nView in Horizon Solar PMS:\n'
                f'https://horizon-solar-pms-production.up.railway.app{task_url}'
            )
            send_notification(
                recipient=assignee,
                message=_at_email_message,
                channels=['in_app', 'whatsapp', 'email'],
                link=task_url,
                subject=f'Task Assigned: {task.task_name} — {project.customer_name}',
                template='assign_task',
                template_params=[project.customer_name, recipient_name, task.task_name, project.customer_name, task_url_abs],
                related_project=project,
                actor=request.user.profile,
            )
        else:
            Task.objects.filter(pk=task.pk).update(assigned_to=None)
        return redirect('project_overview', project_id=project.project_id)

    return render(request, 'projects/task_assign_form.html', {
        'project':    project,
        'task':       task,
        'candidates': candidates,
    })


@login_required
def task_set_due_date(request, project_id, task_id):
    """
    Set the due date on a task.
    PM: can edit any task; cascade-recalculates subsequent tasks.
    Other roles: can only edit tasks assigned to their role, and only when cascade is OFF.
    POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile
    is_pm = _pm_owns_project(request, project)
    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    if not is_pm:
        # Map UserProfile.role to Task.assigned_role (BD → BD / Sales)
        _PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}
        user_task_role = _PROFILE_TO_TASK_ROLE.get(profile.role, profile.role)

        if task.assigned_role != user_task_role:
            raise PermissionDenied

        if project.cascade_scheduling:
            messages.error(request, 'Due dates are managed automatically by cascading scheduling.')
            return redirect('project_overview', project_id=project.project_id)

        # Non-PM: save this task's date only, no cascade ripple
        date_str = request.POST.get('due_date', '').strip()
        if date_str:
            try:
                task.due_date = date.fromisoformat(date_str)
                task.save()
                log_activity(project, profile, f"Updated due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
                messages.success(request, 'Due date updated.')
            except ValueError:
                messages.error(request, 'Invalid date.')
        else:
            task.due_date = None
            task.save()
            log_activity(project, profile, f"Cleared due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
            messages.success(request, 'Due date cleared.')
        return redirect('project_overview', project_id=project.project_id)

    # PM path — cascade-recalculate on date set, clear without cascade
    date_str = request.POST.get('due_date', '').strip()
    if date_str:
        try:
            new_date = date.fromisoformat(date_str)
            count = recalculate_from_task(project, task, new_date, user=request.user)
            messages.success(request, f'Due date updated. {count} task(s) recalculated.')
            log_activity(project, profile, f"Updated due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
        except ValueError:
            messages.error(request, 'Invalid date.')
    else:
        task.due_date = None
        task.save()
        messages.success(request, 'Due date cleared.')
        log_activity(project, profile, f"Updated due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
    return redirect('project_overview', project_id=project.project_id)


# ---------------------------------------------------------------------------
# Vendor Master
# ---------------------------------------------------------------------------

@login_required
def vendor_list(request):
    """
    List all vendors with optional category and active/inactive filters.
    Access: SCM and Admin only (inline role check — not via decorator).
    """
    profile = request.user.profile
    # Inline role check used here (not @role_required) because vendor views
    # are shared between SCM and Admin with identical permission logic
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()

    vendors = Vendor.objects.prefetch_related('categories').order_by('-is_active', 'name')

    category_filter = request.GET.get('category', '')
    active_filter   = request.GET.get('active', '')

    if category_filter:
        vendors = vendors.filter(categories__id=category_filter)
    if active_filter == '1':
        vendors = vendors.filter(is_active=True)
    elif active_filter == '0':
        vendors = vendors.filter(is_active=False)

    return render(request, 'vendors/vendor_list.html', {
        'vendors':          vendors,
        'all_categories':   VendorCategory.objects.all(),
        'category_filter':  category_filter,
        'active_filter':    active_filter,
    })


def _save_vendor_brands(vendor, post_data):
    """
    Replace all VendorBrand entries for vendor with the brand rows from the POST data.
    Expects parallel lists: make_brand[] and brand_category[] from the dynamic form rows.
    Empty brand names are silently skipped.
    """
    brand_names = post_data.getlist('make_brand')
    brand_cats  = post_data.getlist('brand_category')

    vendor.brands.all().delete()
    for name, cat_id in zip(brand_names, brand_cats):
        name = name.strip()
        if not name:
            continue
        VendorBrand.objects.create(
            vendor=vendor,
            make_brand=name,
            category_id=int(cat_id) if cat_id else None,
        )


@login_required
def vendor_add(request):
    """
    Add a new vendor. Warns (but does not block) if a vendor with the same name
    already exists — duplicate names are allowed to handle trading variants.
    VendorBrand entries (make_brand + optional category) are saved from the
    dynamic brand rows submitted alongside the main form.
    Access: SCM and Admin only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()

    if request.method == 'POST':
        form = VendorForm(request.POST)
        if form.is_valid():
            vendor = form.save(commit=False)
            vendor.created_by = profile
            vendor.save()
            form.save_m2m()

            # Save brand rows — each non-empty make_brand value becomes one VendorBrand entry
            _save_vendor_brands(vendor, request.POST)

            duplicate_exists = Vendor.objects.filter(
                name__iexact=vendor.name
            ).exclude(pk=vendor.pk).exists()
            if duplicate_exists:
                messages.warning(
                    request,
                    f'A vendor named "{vendor.name}" already exists. Saved anyway.'
                )
            else:
                messages.success(request, f'Vendor "{vendor.name}" added successfully.')
            return redirect('vendor_list')
    else:
        form = VendorForm()

    return render(request, 'vendors/vendor_form.html', {
        'form':             form,
        'title':            'Add Vendor',
        'vendor_brands':    [],  # no existing brands on a new vendor
        'vendor_categories': VendorCategory.objects.all(),
    })


@login_required
def vendor_edit(request, vendor_id):
    """
    Edit an existing vendor's details. Replaces all VendorBrand entries with
    the brand rows submitted in the form.
    Access: SCM and Admin only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()

    vendor = get_object_or_404(Vendor, pk=vendor_id)

    if request.method == 'POST':
        form = VendorForm(request.POST, instance=vendor)
        if form.is_valid():
            form.save()

            # Replace all brand entries with whatever was submitted in the form
            _save_vendor_brands(vendor, request.POST)

            duplicate_exists = Vendor.objects.filter(
                name__iexact=vendor.name
            ).exclude(pk=vendor.pk).exists()
            if duplicate_exists:
                messages.warning(
                    request,
                    f'A vendor named "{vendor.name}" already exists. Saved anyway.'
                )
            else:
                messages.success(request, f'Vendor "{vendor.name}" updated successfully.')
            return redirect('vendor_list')
    else:
        form = VendorForm(instance=vendor)

    return render(request, 'vendors/vendor_form.html', {
        'form':              form,
        'vendor':            vendor,
        'title':             f'Edit Vendor — {vendor.name}',
        'vendor_brands':     list(vendor.brands.select_related('category').all()),
        'vendor_categories': VendorCategory.objects.all(),
    })


@login_required
def vendor_toggle_status(request, vendor_id):
    """
    Toggle a vendor's active/inactive status. Returns JSON so the UI can update
    the toggle button without a full page reload.
    Access: SCM and Admin only. POST only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()
    if request.method != 'POST':
        return HttpResponseForbidden()

    vendor = get_object_or_404(Vendor, pk=vendor_id)
    vendor.is_active = not vendor.is_active
    vendor.save()
    return JsonResponse({'is_active': vendor.is_active})


# ---------------------------------------------------------------------------
# BOQ Submission
# ---------------------------------------------------------------------------

def _boq_snapshot(boq):
    """
    Return a JSON-safe snapshot of all BOQ items at a workflow transition (Decimal → float).

    make_brand_label is resolved from VendorBrand at snapshot time (vendor × category match)
    and stored directly in the JSON row so the history view is self-contained and does not
    need to re-query VendorBrand for old revisions. Falls back to the vendor company name.
    """
    import decimal as _decimal
    rows = list(boq.items.values(
        'serial_no', 'category', 'description', 'uom',
        'boq_quantity', 'ordered_quantity',
        'make_preference_id', 'make_preference__name', 'ordered_vendor__name',
    ))

    # Resolve brand labels in one query: (vendor_id, category_name) → make_brand
    vendor_ids = {r['make_preference_id'] for r in rows if r['make_preference_id']}
    brand_map  = {}
    if vendor_ids:
        for vb in VendorBrand.objects.filter(vendor_id__in=vendor_ids).select_related('category'):
            brand_map[(vb.vendor_id, vb.category.name if vb.category else None)] = vb.make_brand

    for row in rows:
        vid = row.pop('make_preference_id')  # internal FK — not needed in the stored JSON
        if vid:
            cat = row.get('category')
            row['make_brand_label'] = (
                brand_map.get((vid, cat)) or       # category-specific brand
                brand_map.get((vid, None)) or      # unscoped brand for this vendor
                row.get('make_preference__name')   # company name fallback
            )
        else:
            row['make_brand_label'] = None
        for k, v in list(row.items()):
            if isinstance(v, _decimal.Decimal):
                row[k] = float(v)
    return rows


def _notify_boq_acknowledged(boq, acknowledging_profile, request):
    """Notify PM and the acknowledging SCM user that the BOQ has been acknowledged."""
    items = boq.items.all()
    has_changes = any(
        item.ordered_quantity is not None and item.boq_quantity is not None
        and item.ordered_quantity != item.boq_quantity
        for item in items
    )
    suffix  = ' with changes to ordered quantities' if has_changes else ''
    message = (
        f'BOQ for {boq.project.project_id} ({boq.project.customer_name}) '
        f'has been acknowledged by SCM{suffix}.'
    )
    design_user_name = boq.submitted_by.user.get_full_name() or boq.submitted_by.user.username
    boq_link = f'/projects/{boq.project.project_id}/boq/'
    boq_link_abs = request.build_absolute_uri(boq_link)
    recipients = []
    if boq.project.assigned_pm:
        recipients.append(boq.project.assigned_pm)
    if acknowledging_profile not in recipients:
        recipients.append(acknowledging_profile)
    scm_name = request.user.get_full_name() or request.user.username
    _boq_message = (
        f'The Bill of Quantities for project {boq.project.customer_name} has been acknowledged '
        f'by {scm_name}.\n\n'
        f'Material procurement can now proceed.'
    )
    _boq_email_message = (
        f'{_boq_message}\n\nView in Horizon Solar PMS:\n'
        f'https://horizon-solar-pms-production.up.railway.app{boq_link}'
    )
    for recipient in recipients:
        send_notification(
            recipient=recipient,
            message=_boq_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=boq_link,
            subject=f'BOQ Acknowledged — {boq.project.customer_name}',
            template='boq_acknowledged',
            template_params=[
                boq.project.customer_name,   # [0] header
                scm_name,                    # [1] body[0] — scm_name
            ],
            related_project=boq.project,
            actor=acknowledging_profile,
        )


def _build_vendors_by_category():
    """
    Return dict mapping category name → list of {id, name, make_brand} for active vendors.

    Vendors with VendorBrand entries appear once per brand per relevant category —
    make_brand is shown in the BOQ dropdown; the stored value is still the vendor PK.
    Brands with no category set appear in every supply category for that vendor.
    Vendors with no brands at all fall back to showing the company name.
    """
    result = {}

    # Pre-build vendor → supply categories map (from the M2M on Vendor)
    vendor_supply_cats = {}
    for v in Vendor.objects.filter(is_active=True).prefetch_related('categories'):
        vendor_supply_cats[v.pk] = [cat.name for cat in v.categories.all()]

    # Vendors that have at least one brand entry
    vendors_with_brands = set()
    for vb in (VendorBrand.objects
               .filter(vendor__is_active=True)
               .select_related('vendor', 'category')
               .order_by('vendor__name', 'make_brand')):
        vendors_with_brands.add(vb.vendor_id)
        if vb.category:
            cats = [vb.category.name]
        else:
            # Not scoped — show this brand in every category the vendor supplies
            cats = vendor_supply_cats.get(vb.vendor_id, [])
        for cat in cats:
            result.setdefault(cat, []).append({
                'id':         vb.vendor_id,
                'name':       vb.vendor.name,
                'make_brand': vb.make_brand,
            })

    # Fallback: vendors with no brands appear using their company name
    for v in (Vendor.objects
              .filter(is_active=True)
              .exclude(pk__in=vendors_with_brands)
              .prefetch_related('categories')
              .order_by('name')):
        for cat in v.categories.all():
            result.setdefault(cat.name, []).append({
                'id':         v.pk,
                'name':       v.name,
                'make_brand': '',
            })

    return result


@login_required
def boq_detail(request, project_id):
    """
    BOQ detail page. Handles all BOQ POST actions in a single view:
      Design: save_design, submit_design, add_item, delete_item
      SCM:    save_scm, acknowledge_scm
    BOQ is auto-created for Design users if it doesn't exist yet.
    Access: Design, SCM, PM, Admin.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile
    role    = profile.role

    if role not in ('Design', 'SCM', 'PM', 'Admin'):
        return HttpResponseForbidden()

    # Get or auto-create BOQ for Design users (non-Design sees 'no BOQ yet' state)
    try:
        boq = project.boq
    except BOQ.DoesNotExist:
        boq = None

    if boq is None:
        if role == 'Design':
            boq = BOQ.objects.create(project=project)
            BOQItem.objects.bulk_create([
                BOQItem(boq=boq, **item_data)
                for item_data in get_standard_boq_items()
            ])
        else:
            return render(request, 'projects/boq_detail.html', {
                'project': project,
                'boq':     None,
                'role':    role,
            })

    # Handle POST actions
    if request.method == 'POST':
        action = request.POST.get('action', '')

        # Design can edit in these statuses; Submitted is locked until SCM acts
        _DESIGN_EDITABLE = ('Draft', 'Revision Requested', 'Acknowledged')

        if action in ('save_design', 'submit_design') and role == 'Design' and boq.status in _DESIGN_EDITABLE:
            boq.notes = request.POST.get('notes', '').strip() or None
            boq.save(update_fields=['notes'])
            for item in boq.items.all():
                qty_str    = request.POST.get(f'boq_qty_{item.pk}', '').strip()
                vendor_str = request.POST.get(f'make_pref_{item.pk}', '').strip()
                try:
                    item.boq_quantity = Decimal(qty_str) if qty_str else None
                except InvalidOperation:
                    item.boq_quantity = None
                item.make_preference_id = int(vendor_str) if vendor_str.isdigit() else None
                item.save(update_fields=['boq_quantity', 'make_preference'])

            if action == 'save_design':
                messages.success(request, 'BOQ saved.')
                return redirect('boq_detail', project_id=project_id)

            # submit_design: validate then snapshot and submit
            if not boq.items.filter(boq_quantity__gt=0).exists():
                messages.error(request, 'Enter a quantity for at least one item before submitting.')
                return redirect('boq_detail', project_id=project_id)

            try:
                is_revision  = boq.status in ('Revision Requested', 'Acknowledged')
                new_version  = boq.version + 1 if is_revision else boq.version
                reason       = f'Revision v{new_version}' if is_revision else 'Initial submission'
                snapshot     = _boq_snapshot(boq)
                BOQRevision.objects.create(
                    boq=boq, revised_by=profile,
                    version=new_version, reason=reason, snapshot=snapshot,
                )
                boq.status       = 'Submitted'
                boq.submitted_by = profile
                boq.submitted_at = timezone.now()
                if is_revision:
                    boq.version = new_version
                boq.save(update_fields=['status', 'submitted_by', 'submitted_at', 'version'])
                messages.success(request, 'BOQ submitted to SCM for review.')
                return redirect('boq_detail', project_id=project_id)
            except Exception as exc:
                logger.exception('BOQ submit_design failed for %s', project_id)
                messages.error(request, f'Submit failed: {exc}')
                return redirect('boq_detail', project_id=project_id)

        elif action in ('save_scm', 'acknowledge_scm') and role == 'SCM' and boq.status in ('Submitted', 'Acknowledged'):
            # Save ordered qty, make preference, and ordered vendor regardless of save vs. acknowledge
            for item in boq.items.all():
                qty_str    = request.POST.get(f'ord_qty_{item.pk}', '').strip()
                make_str   = request.POST.get(f'make_pref_{item.pk}', '').strip()
                vendor_str = request.POST.get(f'ord_vendor_{item.pk}', '').strip()
                try:
                    item.ordered_quantity = Decimal(qty_str) if qty_str else None
                except InvalidOperation:
                    item.ordered_quantity = None
                item.make_preference_id = int(make_str) if make_str.isdigit() else None
                item.ordered_vendor_id  = int(vendor_str) if vendor_str.isdigit() else None
                item.save(update_fields=['ordered_quantity', 'make_preference', 'ordered_vendor'])

            if action == 'save_scm':
                messages.success(request, 'Ordered details saved.')
                return redirect('boq_detail', project_id=project_id)

            # acknowledge_scm: snapshot then acknowledge
            snapshot = _boq_snapshot(boq)
            BOQRevision.objects.create(
                boq=boq, revised_by=profile,
                version=boq.version,
                reason=f'SCM Acknowledged v{boq.version}',
                snapshot=snapshot,
            )
            boq.status = 'Acknowledged'
            boq.save(update_fields=['status'])

            # Notify the Design user who submitted this BOQ
            if boq.submitted_by:
                _notify_boq_acknowledged(boq, profile, request)

            messages.success(request, 'BOQ acknowledged.')
            return redirect('boq_detail', project_id=project_id)

        elif action == 'add_item' and role == 'Design' and boq.status in _DESIGN_EDITABLE:
            category    = request.POST.get('new_category', 'Other')
            description = request.POST.get('new_description', '').strip()
            uom         = request.POST.get('new_uom', 'Nos')
            if description:
                last_serial = boq.items.aggregate(Max('serial_no'))['serial_no__max'] or 0
                BOQItem.objects.create(
                    boq=boq, serial_no=last_serial + 1,
                    category=category, description=description,
                    uom=uom, is_standard_item=False,
                )
                messages.success(request, 'Row added.')
            return redirect('boq_detail', project_id=project_id)

        elif action == 'delete_item' and role == 'Design' and boq.status in _DESIGN_EDITABLE:
            item_id = request.POST.get('item_id', '')
            if item_id:
                BOQItem.objects.filter(pk=item_id, boq=boq, is_standard_item=False).delete()
                messages.success(request, 'Row deleted.')
            return redirect('boq_detail', project_id=project_id)

        return redirect('boq_detail', project_id=project_id)

    items = list(boq.items.select_related('make_preference', 'ordered_vendor').order_by('serial_no'))

    # Annotate each item with make_brand_display: the brand label shown in the static
    # (non-editable) Make/Preference cell. Resolved from VendorBrand in one batch query.
    _vendor_ids = {i.make_preference_id for i in items if i.make_preference_id}
    _brand_map  = {}
    if _vendor_ids:
        for vb in VendorBrand.objects.filter(vendor_id__in=_vendor_ids).select_related('category'):
            _brand_map[(vb.vendor_id, vb.category.name if vb.category else None)] = vb.make_brand
    for item in items:
        if item.make_preference_id:
            item.make_brand_display = (
                _brand_map.get((item.make_preference_id, item.category)) or
                _brand_map.get((item.make_preference_id, None)) or
                item.make_preference.name
            )
        else:
            item.make_brand_display = None

    items_by_category = {}
    for item in items:
        items_by_category.setdefault(item.category, []).append(item)

    return render(request, 'projects/boq_detail.html', {
        'project':             project,
        'boq':                 boq,
        'items':               items,
        'items_by_category':   items_by_category.items(),
        'role':                role,
        'vendors_by_category': _build_vendors_by_category(),
        'category_choices':    BOQItem.CATEGORY_CHOICES,
        'uom_choices':         BOQItem.UOM_CHOICES,
        'today':               date.today(),
    })


@login_required
def boq_submit(request, project_id):
    """
    Standalone BOQ submit endpoint (also handled inline in boq_detail).
    Validates at least one item has a quantity, snapshots the BOQ, and moves
    status to Submitted. Increments version on resubmission.
    Access: Design only. POST only.
    """
    if request.method != 'POST':
        return redirect('boq_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role != 'Design':
        return HttpResponseForbidden()

    boq = get_object_or_404(BOQ, project=project)

    if boq.status not in ('Draft', 'Revision Requested'):
        messages.error(request, 'BOQ cannot be submitted in its current state.')
        return redirect('boq_detail', project_id=project_id)

    if not boq.items.filter(boq_quantity__gt=0).exists():
        messages.error(request, 'At least one item must have a BOQ quantity before submitting.')
        return redirect('boq_detail', project_id=project_id)

    is_resubmission = boq.status == 'Revision Requested'
    new_version     = boq.version + 1 if is_resubmission else boq.version
    reason          = f'Revision v{new_version}' if is_resubmission else 'Initial submission'

    snapshot = list(boq.items.values(
        'serial_no', 'category', 'description', 'uom',
        'boq_quantity', 'ordered_quantity',
        'make_preference__name', 'ordered_vendor__name',
    ))

    BOQRevision.objects.create(
        boq=boq, revised_by=profile,
        version=new_version, reason=reason, snapshot=snapshot,
    )

    boq.status       = 'Submitted'
    boq.submitted_by = profile
    boq.submitted_at = timezone.now()
    if is_resubmission:
        boq.version = new_version
    boq.save()

    # Log BOQ submission event
    log_activity(project, profile, f"Submitted BOQ for project: {project.project_id}", entity_type='BOQ', entity_id=boq.pk)
    messages.success(request, 'BOQ submitted to SCM for review.')
    return redirect('boq_detail', project_id=project_id)


@login_required
def boq_acknowledge(request, project_id):
    """
    Standalone BOQ acknowledge endpoint (also handled inline in boq_detail).
    Moves status from Submitted → Acknowledged. Access: SCM only. POST only.
    """
    if request.method != 'POST':
        return redirect('boq_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role != 'SCM':
        return HttpResponseForbidden()

    boq = get_object_or_404(BOQ, project=project)

    if boq.status != 'Submitted':
        messages.error(request, 'Only submitted BOQs can be acknowledged.')
        return redirect('boq_detail', project_id=project_id)

    boq.status = 'Acknowledged'
    boq.save()

    # Log BOQ acknowledgment event
    log_activity(project, profile, f"BOQ Acknowledged for project: {project.project_id}", entity_type='BOQ', entity_id=boq.pk)
    messages.success(request, 'BOQ acknowledged.')
    return redirect('boq_detail', project_id=project_id)


@login_required
def boq_request_revision(request, project_id):
    """
    PM requests a revision on a Submitted or Acknowledged BOQ.
    Snapshots current state before moving to 'Revision Requested'.
    GET renders the reason form; POST processes the request.
    Access: PM only.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role != 'PM':
        return HttpResponseForbidden()

    boq = get_object_or_404(BOQ, project=project)

    if boq.status not in ('Submitted', 'Acknowledged'):
        messages.error(request, 'Revision can only be requested on Submitted or Acknowledged BOQs.')
        return redirect('boq_detail', project_id=project_id)

    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, 'Please provide a reason for the revision request.')
            return render(request, 'projects/boq_request_revision.html', {
                'project': project,
                'boq':     boq,
            })

        try:
            snapshot = _boq_snapshot(boq)
            BOQRevision.objects.create(
                boq=boq, revised_by=profile,
                version=boq.version, reason=f'Revision requested: {reason}',
                snapshot=snapshot,
            )
            boq.status = 'Revision Requested'
            boq.save(update_fields=['status'])
            # Log BOQ revision request event
            log_activity(project, profile, f"BOQ Revision Requested for project: {project.project_id}", entity_type='BOQ', entity_id=boq.pk)
            messages.success(request, 'Revision requested. Design team will be notified.')
            return redirect('boq_detail', project_id=project_id)
        except Exception as exc:
            logger.exception('boq_request_revision failed for %s', project_id)
            messages.error(request, f'Request failed: {exc}')
            return redirect('boq_detail', project_id=project_id)

    return render(request, 'projects/boq_request_revision.html', {
        'project': project,
        'boq':     boq,
    })


@login_required
def boq_history(request, project_id):
    """
    BOQ revision timeline. Annotates each BOQRevision with event_type and badge
    styling based on the reason text. Ordered oldest-first for a readable timeline.
    Access: PM, Design, SCM, Admin.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role not in ('PM', 'Design', 'SCM', 'Admin'):
        return HttpResponseForbidden()

    boq = get_object_or_404(BOQ, project=project)
    # Chronological order so the timeline reads top-to-bottom oldest-first
    raw_revisions = boq.revisions.select_related('revised_by__user').order_by('revised_at')

    def _annotate(rev):
        """Tag a BOQRevision with display metadata (event_type, badge CSS class) based on its reason text."""
        r = rev.reason or ''
        if 'SCM Acknowledged' in r:
            rev.event_type   = 'acknowledged'
            rev.event_label  = 'Acknowledged'
            rev.dot_color    = 'bg-success'
            rev.badge_class  = 'bg-success'
        elif 'Revision requested' in r:
            rev.event_type   = 'revision_requested'
            rev.event_label  = 'Revision Requested'
            rev.dot_color    = 'bg-warning'
            rev.badge_class  = 'bg-warning text-dark'
        elif r.startswith('Revision v'):
            rev.event_type   = 'resubmitted'
            rev.event_label  = 'Resubmitted'
            rev.dot_color    = 'bg-primary'
            rev.badge_class  = 'bg-primary'
        else:
            rev.event_type   = 'submitted'
            rev.event_label  = 'Submitted'
            rev.dot_color    = 'bg-primary'
            rev.badge_class  = 'bg-primary'
        return rev

    revisions = [_annotate(rev) for rev in raw_revisions]

    return render(request, 'projects/boq_history.html', {
        'project':   project,
        'boq':       boq,
        'revisions': revisions,
    })


@login_required
def notifications_view(request):
    """
    List the user's notifications and mark them all as read.
    Both GET and POST mark notifications read — visiting the page clears the badge.
    Access: any authenticated user (their own notifications only).
    """
    profile       = request.user.profile
    notifications = profile.notifications.all()  # already ordered -created_at

    if request.method == 'POST':
        # Mark all as read
        profile.notifications.filter(is_read=False).update(is_read=True)
        return redirect('notifications')

    # Mark all as read on GET too (visiting the page clears the badge)
    profile.notifications.filter(is_read=False).update(is_read=True)

    return render(request, 'projects/notifications.html', {
        'notifications':      notifications,
        'user_dashboard_url': get_user_dashboard(request.user),
    })


# ---------------------------------------------------------------------------
# Finance milestone actions
# ---------------------------------------------------------------------------

@login_required
@role_required(['Finance'])
def milestone_invoice(request, project_id, milestone_pk):
    """
    Mark a Pending milestone as Invoiced and record today as invoice_date.
    Access: Finance only. POST only.
    """
    if request.method != 'POST':
        return redirect('dashboard_finance')

    milestone = get_object_or_404(
        PaymentMilestone, pk=milestone_pk, project__project_id=project_id
    )
    if milestone.status != 'Pending':
        messages.error(request, 'Only Pending milestones can be marked as Invoiced.')
        return redirect('dashboard_finance')

    milestone.status       = 'Invoiced'
    milestone.invoice_date = date.today()
    milestone.save(update_fields=['status', 'invoice_date'])
    # Log milestone invoiced — project accessed via FK to avoid extra query
    log_activity(milestone.project, request.user.profile, f"Marked {milestone.milestone_name} as Invoiced", entity_type='Milestone', entity_id=milestone.pk)
    messages.success(request, f'{milestone.milestone_name} marked as Invoiced.')
    return redirect('dashboard_finance')


@login_required
@role_required(['Finance'])
def milestone_receive(request, project_id, milestone_pk):
    """
    Mark an Invoiced milestone as Received. Records amount_received and variance_reason.
    Auto-sets variance_reason='Overpayment' when amount_received > amount and no reason given.
    Access: Finance only. POST only.
    """
    if request.method != 'POST':
        return redirect('dashboard_finance')

    milestone = get_object_or_404(
        PaymentMilestone, pk=milestone_pk, project__project_id=project_id
    )
    if milestone.status == 'Received':
        messages.error(request, 'Milestone is already marked as Received.')
        return redirect('dashboard_finance')

    amount_received_str = request.POST.get('amount_received', '').strip()
    if not amount_received_str:
        messages.error(request, 'Amount received is required.')
        return redirect('dashboard_finance')

    try:
        amount_received = Decimal(amount_received_str)
    except InvalidOperation:
        messages.error(request, 'Invalid amount value.')
        return redirect('dashboard_finance')

    variance_reason = request.POST.get('variance_reason', '').strip()
    if milestone.amount and amount_received > milestone.amount and not variance_reason:
        variance_reason = 'Overpayment'

    milestone.status          = 'Received'
    milestone.received_date   = date.today()
    milestone.amount_received = amount_received
    milestone.variance_reason = variance_reason
    milestone.save(update_fields=['status', 'received_date', 'amount_received', 'variance_reason'])
    # Log milestone received — project accessed via FK to avoid extra query
    log_activity(milestone.project, request.user.profile, f"Marked {milestone.milestone_name} as Received", entity_type='Milestone', entity_id=milestone.pk)

    # Bidirectional sync: PaymentMilestone Received → Finance confirmation task Done.
    _MILESTONE_TO_FINANCE_TASK = {
        'M1': 'Advance Payment Confirmation',
        'M2': 'Finance Confirmation',
        'M3': '100% Payment Confirmation',
    }
    _sync_task_name = _MILESTONE_TO_FINANCE_TASK.get(milestone.milestone_name)
    if _sync_task_name:
        try:
            Task.objects.filter(
                phase__project=milestone.project,
                task_name=_sync_task_name,
                status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
            ).update(status=Task.DONE, completed_at=timezone.now())
        except Exception:
            pass  # Non-critical — never block the milestone update

    messages.success(request, f'{milestone.milestone_name} marked as Received.')
    return redirect('dashboard_finance')


@login_required
@role_required(['PM'])
def milestone_create(request, project_id):
    """
    Create the standard M1/M2/M3 milestones for a project if none exist yet.
    Milestones are normally created automatically during project_activate —
    this view exists as a fallback for projects activated before milestones were added.
    Access: assigned PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    if not _pm_owns_project(request, project):
        raise Http404

    if not project.milestones.exists():
        milestone_defaults = [
            ('M1', 'On Survey Completion'),
            ('M2', 'On Material Supply'),
            ('M3', 'On Commissioning'),
        ]
        for name, desc in milestone_defaults:
            PaymentMilestone.objects.create(
                project=project,
                milestone_name=name,
                milestone_description=desc,
                created_by=request.user.profile,
            )
        messages.success(request, 'Payment milestones created.')
    else:
        messages.info(request, 'Milestones already exist for this project.')
    return redirect('project_overview', project_id=project_id)


# ---------------------------------------------------------------------------
# Payment Requests
# ---------------------------------------------------------------------------

@login_required
def raise_payment_request(request, project_id):
    """SCM raises a payment request against a vendor invoice. SCM role only. POST."""
    if request.method != 'POST':
        return redirect('dashboard_scm')

    # Raising a payment request is SCM-only — other roles get 403
    profile = request.user.profile
    if profile.role != 'SCM':
        return HttpResponse(status=403)

    project = get_object_or_404(Project, project_id=project_id)

    vendor_id      = request.POST.get('vendor_id', '').strip()
    boq_item_id    = request.POST.get('boq_item_id', '').strip()
    invoice_number = request.POST.get('invoice_number', '').strip()
    amount_str     = request.POST.get('amount', '').strip()
    note           = request.POST.get('note', '').strip()
    invoice_file   = request.FILES.get('invoice_document')

    # Server-side validation — invoice_document is mandatory (hard business rule, not a UI nicety)
    errors = []
    if not vendor_id:
        errors.append('Vendor is required.')
    if not boq_item_id:
        errors.append('BOQ item is required.')
    if not invoice_number:
        errors.append('Invoice number is required.')
    if not amount_str:
        errors.append('Amount is required.')
    if not invoice_file:
        errors.append('Invoice document is required.')

    if errors:
        messages.error(request, ' '.join(errors))
        return redirect('dashboard_scm')

    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return redirect('dashboard_scm')

    vendor = get_object_or_404(Vendor, pk=vendor_id, is_active=True)

    # BOQ item dropdown scoped to THIS project only — never show another
    # project's BOQ items in the raise-request form.
    boq_item = get_object_or_404(BOQItem, pk=boq_item_id, boq__project=project)

    # Upload invoice document to Supabase — reuse same pattern as ProjectDocument/TaskAttachment
    try:
        from .supabase_storage import get_supabase_client
        client = get_supabase_client()
    except (ValueError, ImportError) as exc:
        messages.error(request, f"Upload service unavailable. ({exc})")
        return redirect('dashboard_scm')

    supabase_path = (
        f"payment-requests/{project.project_id}/"
        f"{_uuid.uuid4()}_{invoice_file.name}"
    )
    try:
        _validate_and_upload(invoice_file, client, settings.SUPABASE_BUCKET, supabase_path)
        invoice_document_url = (
            f"{settings.SUPABASE_URL}/storage/v1/object/public/"
            f"{settings.SUPABASE_BUCKET}/{supabase_path}"
        )
    except ValueError as exc:
        messages.error(request, f"Invoice upload failed: {exc}")
        return redirect('dashboard_scm')

    pr = PaymentRequest.objects.create(
        project=project,
        vendor=vendor,
        boq_item=boq_item,
        invoice_number=invoice_number,
        invoice_document_name=invoice_file.name,
        invoice_document_url=invoice_document_url,
        invoice_document_path=supabase_path,
        amount=amount,
        note=note,
        requested_by=request.user,
        status=PaymentRequest.PENDING,
    )

    # Confirm actions are state-mutating — log them so they surface in the
    # project's Recent Activity panel for audit trail.
    log_activity(
        project, profile,
        f"Raised payment request to {vendor.name}: ₹{amount} (Invoice {invoice_number})",
        entity_type='PaymentRequest', entity_id=pr.pk,
    )
    messages.success(request, f'Payment request raised for ₹{amount} to {vendor.name}.')
    return redirect('dashboard_scm')


@login_required
def confirm_payment_request(request, project_id, request_id):
    """Finance confirms a vendor payment has been made. Finance role only. POST."""
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    # Confirming payment is Finance-only — SCM and PM see status read-only
    profile = request.user.profile
    if profile.role != 'Finance':
        return HttpResponse(status=403)

    project = get_object_or_404(Project, project_id=project_id)
    pr = get_object_or_404(
        PaymentRequest.objects.select_related('vendor', 'boq_item'),
        pk=request_id, project=project, status=PaymentRequest.PENDING,
    )

    payment_date_str  = request.POST.get('payment_date', '').strip()
    payment_reference = request.POST.get('payment_reference', '').strip()

    if not payment_date_str:
        messages.error(request, 'Payment date is required.')
        return redirect('project_overview', project_id=project_id)

    try:
        payment_date = date.fromisoformat(payment_date_str)
    except ValueError:
        messages.error(request, 'Invalid payment date.')
        return redirect('project_overview', project_id=project_id)

    pr.status            = PaymentRequest.CONFIRMED
    pr.payment_date      = payment_date
    pr.payment_reference = payment_reference
    pr.confirmed_by      = request.user
    pr.save(update_fields=['status', 'payment_date', 'payment_reference', 'confirmed_by'])

    # Confirm actions are state-mutating — log them so they surface in the
    # project's Recent Activity panel for audit trail.
    log_activity(
        project, profile,
        f"Confirmed payment to {pr.vendor}: ₹{pr.amount} (Ref: {payment_reference or '—'})",
        entity_type='PaymentRequest', entity_id=pr.pk,
    )

    boq_desc = pr.boq_item.description if pr.boq_item else '(item)'
    seen_pks = set()
    invoice_recipients = list(UserProfile.objects.filter(role='SCM', is_active=True))
    if project.assigned_pm:
        invoice_recipients.append(project.assigned_pm)
    invoice_recipients += list(UserProfile.objects.filter(role='CEO', is_active=True))
    _ip_link = f'/projects/{project.project_id}/overview/'
    _ip_message = (
        f'Payment has been confirmed for project {project.customer_name}.\n\n'
        f'Vendor: {pr.vendor.name}. Invoice: {pr.invoice_number}. '
        f'Amount: Rs. {pr.amount}. Item: {boq_desc}.\n\n'
        f'Records have been updated.'
    )
    _ip_email_message = (
        f'{_ip_message}\n\nView in Horizon Solar PMS:\n'
        f'https://horizon-solar-pms-production.up.railway.app{_ip_link}'
    )
    for recipient in invoice_recipients:
        if recipient.pk in seen_pks:
            continue
        seen_pks.add(recipient.pk)
        send_notification(
            recipient=recipient,
            message=_ip_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=_ip_link,
            subject=f'Invoice Payment Confirmed — {project.customer_name}',
            template='invoice_paid',
            template_params=[project.customer_name, boq_desc, pr.invoice_number, str(pr.amount), pr.vendor.name],
            related_project=project,
            actor=profile,
        )

    messages.success(request, f'Payment of ₹{pr.amount} to {pr.vendor} confirmed.')
    return redirect('project_overview', project_id=project_id)


# ---------------------------------------------------------------------------
# BD dashboard
# ---------------------------------------------------------------------------

@login_required
@role_required(['BD'])  # UserProfile.role = 'BD'; confirmed by Trigger 2
def dashboard_bd(request):
    """Sales & BD dashboard. One card per project, combined urgency circle. BD role only."""
    today = date.today()

    # Trigger 4: BD is department-level — no assigned_bd field on Project model confirmed.
    # BD sees all active/in-progress projects, same as Finance and SCM dashboards.
    #
    # Trigger 3: milestones on PaymentMilestone (related_name='milestones') —
    # prefetch reused exactly from Finance dashboard session; same pattern, same model.
    #
    # Trigger 6: annotate open issue count using Open+InProgress — same definition as
    # PM dashboard's open_issue_count. "Blocked issue" is BD's term for the same thing;
    # there is no separate "blocked" status on Issue.
    projects_qs = (
        Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
        .prefetch_related(
            'milestones',
            Prefetch(
                'phases',
                queryset=ProjectPhase.objects.prefetch_related('tasks').order_by('phase_order'),
            ),
        )
        .select_related('assigned_pm__user')
        .annotate(
            open_issue_count=Count(
                'issues',
                filter=Q(issues__status__in=[Issue.OPEN, Issue.IN_PROGRESS]),
                distinct=True,
            )
        )
        .order_by('project_id')
    )

    # Trigger 5: No orc_status field on Project model — derived from BD task completion.
    # Task with assigned_role='BD / Sales' in "Sales & Documentation" phase represents ORC.
    # One batch query across all active projects avoids N+1 in the loop below.
    orc_done_project_ids = set(
        Task.objects.filter(
            phase__project__status__in=['Active', 'In Progress'],
            assigned_role=Task.BD,  # = 'BD / Sales'
            status=Task.DONE,
        ).values_list('phase__project_id', flat=True).distinct()
    )

    project_rows = []
    for project in projects_qs:
        # is_delayed: same view-computed logic as PM/SE/Finance/Design dashboards
        is_delayed = bool(
            project.target_commissioning_date
            and project.target_commissioning_date < today
        )
        delay_days = (
            (today - project.target_commissioning_date).days
            if is_delayed else None
        )

        # Current phase: first phase with an incomplete task; uses prefetched data — no extra query
        current_phase_name = None
        for phase in project.phases.all():
            for task in phase.tasks.all():
                if task.status != Task.DONE:
                    current_phase_name = phase.phase_name
                    break
            if current_phase_name:
                break

        # Trigger 3: milestone summary — same prefetch as Finance dashboard session.
        # milestones_awaiting = 'Invoiced' status: Finance has invoiced, BD nudges client for receipt.
        # (Finance's milestones_awaiting = 'Pending'; BD's perspective is post-invoice.)
        milestones_total    = 0
        milestones_received = 0
        milestones_awaiting = 0
        milestones_list     = []
        for m in project.milestones.all():
            milestones_total += 1
            if m.status == PaymentMilestone.RECEIVED:
                milestones_received += 1
            elif m.status == PaymentMilestone.INVOICED:
                # Invoice sent to client; BD follows up for payment receipt
                milestones_awaiting += 1
            # Variance for display: positive = short (expected > received), negative = excess
            if m.amount is not None and m.amount_received is not None:
                _var = m.amount - m.amount_received
                _var_abs = abs(_var)
            else:
                _var = None
                _var_abs = None
            milestones_list.append({
                'pk':                   m.pk,
                'milestone_name':       m.milestone_name,
                'milestone_description': m.milestone_description,
                'amount':               m.amount,
                'amount_received':      m.amount_received,
                'status':               m.status,
                'variance':             _var,
                'variance_abs':         _var_abs,
            })

        # Trigger 6: annotated open_issue_count = Open or In Progress — same as PM/SE dashboards
        blocked_issue_count = project.open_issue_count

        # Trigger 5: ORC derived from BD task completion — no orc_status field on Project.
        # orc_overdue hardcoded False — no SLA threshold defined; same pattern as Design dashboard.
        orc_status  = 'uploaded' if project.pk in orc_done_project_ids else 'pending'
        orc_overdue = False  # TODO: wire to real SLA once thresholds are defined in Claude Chat

        # Urgency formula from spec: blocked issues + invoiced-not-received milestones + overdue ORC
        urgency_count = (
            blocked_issue_count
            + milestones_awaiting
            + (1 if orc_status == 'pending' and orc_overdue else 0)
        )

        # Precedence: Blocked > Delayed > On-time, computed server-side.
        # Blocked overrides Delayed even when both are true — worse state wins.
        # Confirmed in Claude Chat, 19-June session.
        if blocked_issue_count > 0:
            status_badge = 'blocked'
        elif is_delayed:
            status_badge = 'delayed'
        else:
            status_badge = 'on_time'

        pm_name = None
        if project.assigned_pm:
            # Trigger 7: assigned_pm FK → UserProfile; full name with username fallback
            pm_name = (
                project.assigned_pm.user.get_full_name()
                or project.assigned_pm.user.username
            )

        project_rows.append({
            'project':             project,
            'project_id':          project.project_id,
            'customer_name':       project.customer_name,
            'phase':               current_phase_name,
            'pm_name':             pm_name,
            'is_delayed':          is_delayed,
            'delay_days':          delay_days,
            'orc_status':          orc_status,
            'orc_overdue':         orc_overdue,
            'milestones':          milestones_list,
            'milestones_total':    milestones_total,
            'milestones_received': milestones_received,
            'milestones_awaiting': milestones_awaiting,
            'blocked_issue_count': blocked_issue_count,
            'urgency_count':       urgency_count,
            'status_badge':        status_badge,
        })

    # Most-urgent-first; all-clear projects sink to bottom
    project_rows.sort(key=lambda r: r['urgency_count'], reverse=True)

    # Top-level summary — derived from per-project data, no extra queries
    total_blocked_projects    = sum(1 for r in project_rows if r['blocked_issue_count'] > 0)
    total_orc_overdue         = sum(1 for r in project_rows if r['orc_overdue'])
    total_milestones_awaiting = sum(r['milestones_awaiting'] for r in project_rows)

    return render(request, 'dashboard/bd.html', {
        'bd_first_name':             request.user.first_name,
        'project_rows':              project_rows,
        'total_blocked_projects':    total_blocked_projects,
        'total_orc_overdue':         total_orc_overdue,
        'total_milestones_awaiting': total_milestones_awaiting,
    })


@login_required
@role_required(['BD', 'PM'])
def set_milestone_amounts(request, project_id):
    """
    Set M1/M2/M3 agreed amounts for a project. Called by BD on Phase 1 Task 1 Done gate
    and by the BD dashboard inline edit pencil.
    Null values for a milestone key = skip that milestone (used for single-milestone edits).
    POST body: JSON {m1_amount, m2_amount, m3_amount} where each is a number or null.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'}, status=405)

    project = get_object_or_404(Project, project_id=project_id)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid request body.'})

    amounts = {}
    for name, key in [('M1', 'm1_amount'), ('M2', 'm2_amount'), ('M3', 'm3_amount')]:
        raw = data.get(key)
        if raw is None:
            continue  # null in JSON = skip this milestone
        raw_str = str(raw).strip()
        if raw_str == '':
            return JsonResponse({'success': False, 'error': 'All three milestone amounts are required.'})
        try:
            val = Decimal(raw_str)
            if val < 0:
                return JsonResponse({'success': False, 'error': 'Amounts must be 0 or greater.'})
            amounts[name] = val
        except InvalidOperation:
            return JsonResponse({'success': False, 'error': 'All three milestone amounts are required.'})

    if not amounts:
        return JsonResponse({'success': False, 'error': 'No amounts provided.'})

    # Contract value check — only when all three milestones will be non-null after this update
    existing = {m.milestone_name: m.amount for m in project.milestones.all() if m.amount is not None}
    merged = dict(existing)
    merged.update(amounts)
    if all(merged.get(n) is not None for n in ('M1', 'M2', 'M3')):
        total = merged['M1'] + merged['M2'] + merged['M3']
        contract = project.contract_value
        if contract is not None and total != contract:
            return JsonResponse({
                'success': False,
                'error': (
                    f'Milestone amounts must sum to ₹{contract:,.0f} (contract value). '
                    f'Current total: ₹{total:,.0f}. '
                    f'Difference: ₹{abs(contract - total):,.0f}.'
                ),
            })

    try:
        with transaction.atomic():
            for name, amount in amounts.items():
                PaymentMilestone.objects.update_or_create(
                    project=project, milestone_name=name,
                    defaults={'amount': amount},
                )
        log_activity(
            project, request.user.profile,
            'Set milestone amounts: ' + ', '.join(f'{n}=₹{a}' for n, a in amounts.items()),
            entity_type='Milestone',
        )
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# Individual project overview
# ---------------------------------------------------------------------------

@login_required
def project_overview(request, project_id):
    """
    Combined project page: info card, status blocks, documents, and phase/task list.
    Merges the former project_detail (task management) and project_overview (status summary)
    into a single URL. Access: all roles; PM and SE isolation applies.
    """
    project = get_object_or_404(
        Project.objects.select_related(
            'assigned_pm__user', 'assigned_site_engineer__user',
            'assigned_design__user', 'created_by',
        ),
        project_id=project_id,
    )
    profile = request.user.profile
    role    = profile.role

    # Role isolation
    if role == 'PM' and project.assigned_pm != profile:
        raise Http404
    if role == 'Site Engineer' and project.assigned_site_engineer != profile:
        raise Http404

    is_assigned_pm    = (project.assigned_pm is not None and project.assigned_pm.user == request.user)
    can_assign_design = is_assigned_pm and project.status in ('Active', 'In Progress')

    # Handle POST actions — PM for all actions; Finance limited to update_milestone
    if request.method == 'POST' and (is_assigned_pm or (role == 'Finance' and request.POST.get('action') == 'update_milestone')):
        action = request.POST.get('action', '')

        if action == 'update_milestone':
            milestone_pk = request.POST.get('milestone_pk', '').strip()
            if milestone_pk:
                try:
                    milestone = project.milestones.get(pk=milestone_pk)
                    _actor_role = getattr(getattr(request.user, 'profile', None), 'role', None)

                    if _actor_role == 'Finance':
                        milestone.milestone_description = request.POST.get('milestone_description', '').strip()
                        _save_fields = ['milestone_description']
                        _amount_received_str = request.POST.get('amount_received', '').strip()
                        if _amount_received_str and milestone.status != 'Received':
                            try:
                                milestone.amount_received = Decimal(_amount_received_str)
                                milestone.status        = 'Received'
                                milestone.received_date = date.today()
                                _save_fields += ['amount_received', 'status', 'received_date']
                            except InvalidOperation:
                                pass
                        milestone.save(update_fields=_save_fields)
                        log_activity(project, request.user.profile, f"Updated {milestone.milestone_name}", entity_type='Milestone', entity_id=milestone.pk)
                        if 'status' in _save_fields:
                            _MILESTONE_TO_FINANCE_TASK = {
                                'M1': 'Advance Payment Confirmation',
                                'M2': 'Finance Confirmation',
                                'M3': '100% Payment Confirmation',
                            }
                            _sync_task_name = _MILESTONE_TO_FINANCE_TASK.get(milestone.milestone_name)
                            if _sync_task_name:
                                try:
                                    Task.objects.filter(
                                        phase__project=project,
                                        task_name=_sync_task_name,
                                        status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
                                    ).update(status=Task.DONE, completed_at=timezone.now())
                                except Exception:
                                    pass
                    else:
                        milestone.milestone_description = request.POST.get('milestone_description', '').strip()
                        amount_str   = request.POST.get('amount', '').strip()
                        due_date_str = request.POST.get('due_date', '').strip()
                        try:
                            milestone.amount = Decimal(amount_str) if amount_str else None
                        except InvalidOperation:
                            milestone.amount = None
                        try:
                            milestone.due_date = date.fromisoformat(due_date_str) if due_date_str else None
                        except ValueError:
                            milestone.due_date = None
                        milestone.save(update_fields=['milestone_description', 'amount', 'due_date'])
                    messages.success(request, f'{milestone.milestone_name} updated.')
                except PaymentMilestone.DoesNotExist:
                    messages.error(request, 'Milestone not found.')
            return redirect('project_overview', project_id=project.project_id)

        if action == 'assign_design' and can_assign_design:
            design_id = request.POST.get('assigned_design', '').strip()
            if design_id:
                try:
                    design_user = UserProfile.objects.get(pk=design_id, role='Design', is_active=True)
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Invalid design user selected.')
                    return redirect('project_overview', project_id=project.project_id)
                project.assigned_design = design_user
                project.save(update_fields=['assigned_design'])
                Task.objects.filter(
                    phase__project=project,
                    assigned_role=Task.DESIGN,
                    status__in=['Not Started', 'In Progress'],
                ).update(assigned_to=design_user)
            else:
                project.assigned_design = None
                project.save(update_fields=['assigned_design'])
                Task.objects.filter(
                    phase__project=project,
                    assigned_role=Task.DESIGN,
                    status__in=['Not Started', 'In Progress'],
                ).update(assigned_to=None)
            messages.success(request, 'Design member updated.')
            return redirect('project_overview', project_id=project.project_id)

    # BOQ status + last event
    try:
        project.boq_status     = project.boq.status
        project.boq_last_event = project.boq.revisions.order_by('-revised_at').first()
        project.boq_url        = f'/projects/{project.project_id}/boq/'
    except Exception:
        project.boq_status     = None
        project.boq_last_event = None
        project.boq_url        = None

    # BOQ revision history for expanded block
    boq_revision_history = []
    try:
        boq_revision_history = list(
            project.boq.revisions.select_related('revised_by__user').order_by('-revised_at')
        )
    except Exception:
        pass

    # Milestones with variance (positive = short / expected > received; negative = excess)
    milestones = list(project.milestones.all())
    for m in milestones:
        if m.amount is not None and m.amount_received is not None:
            m.variance = m.amount - m.amount_received
            m.variance_abs = abs(m.variance)
        else:
            m.variance = None
            m.variance_abs = None

    # Dict of existing amounts keyed by milestone_name — used by BD modal pre-fill
    milestone_amounts = {m.milestone_name: str(m.amount) if m.amount is not None else '' for m in milestones}

    # Phases + task completion percentages (empty for Draft projects)
    phases = []
    phase_data_json = []
    if project.status != 'Draft':
        phases = list(
            ProjectPhase.objects.filter(project=project)
            .prefetch_related('tasks')
            .order_by('phase_order')
        )
        for phase in phases:
            tasks = list(phase.tasks.all())
            if not tasks:
                continue
            internal       = [t for t in tasks if t.task_type == 'Internal']
            internal_done  = sum(1 for t in internal if t.status == 'Done')
            internal_total = len(internal)
            pct            = int(internal_done / internal_total * 100) if internal_total else 0
            ext_pending    = sum(1 for t in tasks if t.task_type == 'External' and t.status != 'Done')
            phase_data_json.append({
                'pk':             phase.pk,
                'pct':            pct,
                'internal_done':  internal_done,
                'internal_total': internal_total,
                'ext_pending':    ext_pending,
            })

    # Recent activity from ActivityLog (authoritative audit trail)
    recent_activity = list(
        ActivityLog.objects.filter(project=project)
        .select_related('actor__user')
        .order_by('-timestamp')[:8]
    )

    documents = project.documents.filter(is_deleted=False) if project.status != 'Draft' else []

    project_issues = (
        Issue.objects.filter(project=project)
        .select_related('raised_by__user', 'assigned_to__user', 'task')
    )
    all_profiles = UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name')

    # Delivery challans with per-DC severity-aware summary text
    delivery_challans = list(
        project.delivery_challans
        .select_related('vendor', 'created_by__user')
        .prefetch_related('line_items')
        .all()
    )
    for dc in delivery_challans:
        li_list   = list(dc.line_items.all())
        total     = len(li_list)
        confirmed = [li for li in li_list if li.received_quantity is not None]
        n_conf    = len(confirmed)

        if not total:
            dc.line_items_summary = "No items logged"
        elif n_conf == 0:
            dc.line_items_summary = f"0 of {total} item{'s' if total > 1 else ''} confirmed"
        else:
            total_ordered  = sum(li.ordered_quantity for li in li_list)
            total_received = sum(li.received_quantity for li in confirmed)
            total_damaged  = sum(li.damaged_quantity for li in confirmed)
            # Use the DC's already-computed severity status for the summary label
            if dc.status == DeliveryChallan.RECEIVED:
                dc.line_items_summary = f"All {n_conf} item{'s' if n_conf > 1 else ''} received"
            elif dc.status == DeliveryChallan.REJECTED:
                # Severe: shortfall + damage, or nothing received
                dc.line_items_summary = (
                    f"Severe — {total_received:.0f}/{total_ordered:.0f} units"
                    + (f", {total_damaged} damaged" if total_damaged else "")
                )
            else:
                # Partially Received (AMBER): shortfall or damage, not both stacked
                if total_damaged:
                    dc.line_items_summary = (
                        f"Partial — {total_received:.0f}/{total_ordered:.0f} units, {total_damaged} damaged"
                    )
                else:
                    dc.line_items_summary = (
                        f"Partial — {total_received:.0f} of {total_ordered:.0f} units received"
                    )

    # Per-category material status (summary badge + detail quantities)
    material_status = get_material_status(project)

    cat_rows = (
        DCLineItem.objects.filter(challan__project=project)
        .values('boq_category')
        .annotate(
            total_ordered=Sum('ordered_quantity'),
            total_received=Sum('received_quantity'),
            total_damaged=Sum('damaged_quantity'),
            # Use damaged_quantity (precise numeric) — not the coarse condition string
            damage_count=Count('pk', filter=Q(damaged_quantity__gt=0)),
        )
    )
    material_status_by_category = []
    for row in cat_rows:
        received      = row['total_received'] or 0
        ordered       = row['total_ordered'] or 0
        total_damaged = row['total_damaged'] or 0
        has_damage    = row['damage_count'] > 0
        if received == 0:
            cat_status = 'Pending'
        elif received >= ordered and not has_damage:
            cat_status = 'Received'
        else:
            cat_status = 'Partial'
        material_status_by_category.append({
            'name':              row['boq_category'],
            'status':            cat_status,
            'received_quantity': received,
            'ordered_quantity':  ordered,
            'has_damage':        has_damage,
            'total_damaged':     total_damaged,
        })

    # Design assignment candidates (PM only, for assign-design dropdown)
    # Task.ROLE_CHOICES uses 'BD / Sales' but UserProfile.role stores 'BD'
    _TASK_TO_PROFILE_ROLE = {'BD / Sales': 'BD'}
    candidates_by_role = {}
    design_candidates  = UserProfile.objects.none()
    if is_assigned_pm:
        for role_key, _ in Task.ROLE_CHOICES:
            profile_role = _TASK_TO_PROFILE_ROLE.get(role_key, role_key)
            qs = UserProfile.objects.filter(role=profile_role, is_active=True).select_related('user')
            candidates_by_role[role_key] = [
                {'pk': p.pk, 'name': p.user.get_full_name() or p.user.username}
                for p in qs
            ]
        design_candidates = UserProfile.objects.filter(role='Design', is_active=True).select_related('user')

    dc_vendors = Vendor.objects.filter(is_active=True).order_by('name') if role == 'SCM' else []

    # Normalise UserProfile role → Task.ROLE_CHOICES value for template comparisons
    _PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}
    user_task_role = _PROFILE_TO_TASK_ROLE.get(role, role)

    # Cascade scheduling context — PM-only feature gate check
    _sys = SystemSettings.get()
    show_cascade_option = (_sys.cascade_scheduling_enabled and role == 'PM' and is_assigned_pm)

    # Payment requests for this project — Finance sees confirm actions, PM sees read-only
    # The queryset is shared; template role-gates control which actions are rendered.
    payment_requests = []
    if role in ('Finance', 'PM', 'SCM', 'Admin'):
        payment_requests = list(
            PaymentRequest.objects.filter(project=project)
            .select_related('vendor', 'boq_item', 'requested_by', 'confirmed_by')
            .order_by('-requested_date')
        )

    return render(request, 'projects/project_overview.html', {
        'project':                     project,
        'milestones':                  milestones,
        'milestone_amounts':           milestone_amounts,
        'phases':                      phases,
        'phase_data_json':             json.dumps(phase_data_json),
        'recent_activity':             recent_activity,
        'role':                        role,
        'user_role':                   role,
        'user_task_role':              user_task_role,
        'user_profile':                profile,
        'documents':                   documents,
        'project_issues':              project_issues,
        'all_profiles':                all_profiles,
        'delivery_challans':           delivery_challans,
        'material_status':             material_status,
        'material_status_by_category': material_status_by_category,
        'dc_vendors':                  dc_vendors,
        'dc_category_choices':         DCLineItem.CATEGORY_CHOICES,
        'dc_condition_choices':        DCLineItem.CONDITION_CHOICES,
        'is_assigned_pm':              is_assigned_pm,
        'can_assign_design':           can_assign_design,
        'design_candidates':           design_candidates,
        'task_status_choices':         Task.STATUS_CHOICES,
        'candidates_by_role':          candidates_by_role,
        'boq_revision_history':        boq_revision_history,
        'today':                       date.today(),
        'payment_requests':            payment_requests,
        'user_dashboard_url':          get_user_dashboard(request.user),
        'show_cascade_option':         show_cascade_option,
    })


# ---------------------------------------------------------------------------
# Zoho CRM Webhook
# ---------------------------------------------------------------------------

def _safe_decimal(value):
    """Strip non-numeric characters and convert to Decimal. Returns None on failure or empty input."""
    if not value:
        return None
    cleaned = re.sub(r'[^\d.]', '', str(value))
    try:
        return Decimal(cleaned)
    except Exception:
        return None


@csrf_exempt
def zoho_deal_closed_webhook(request):
    """
    Receive a Zoho CRM webhook when a deal reaches 'Closed Won'.
    Creates a Draft project from the deal fields.
    Security: token validated from X-Webhook-Token header or ?token= / ?secret= param.
    Duplicate guard: skips if a project with the same zoho_deal_id already exists.
    Always returns HTTP 200 even on application errors — non-200 causes Zoho to retry
    and would create duplicate projects.
    Access: public (unauthenticated), token-protected.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    # Token validation — accept header, ?token=, or ?secret= (Zoho default param name)
    token = (
        request.headers.get('X-Webhook-Token', '')
        or request.GET.get('token', '')
        or request.GET.get('secret', '')
    )
    expected = settings.ZOHO_WEBHOOK_SECRET
    if not expected or token != expected:
        ip = request.META.get('REMOTE_ADDR', 'unknown')
        logger.warning('Webhook rejected: invalid token from IP %s', ip)
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return HttpResponse(status=400)

    # Zoho payload structure varies between webhook versions:
    # flat root (fields at top level) or wrapped in data[]/data[0].Deal
    data_wrapper = payload.get('data')
    if data_wrapper is not None:
        if isinstance(data_wrapper, list):
            data_wrapper = data_wrapper[0] if data_wrapper else {}
        deal = data_wrapper.get('Deal', data_wrapper)
    else:
        deal = payload  # flat root — fields are at the top level

    stage = deal.get('Stage', '')
    # Ignore all stages except Closed Won — return 200 so Zoho doesn't retry
    if stage != 'Closed Won':
        return HttpResponse(status=200)

    record_id = str(deal.get('id', '') or deal.get('Record_Id', '') or deal.get('zoho_deal_id', '')).strip()

    # Duplicate guard
    if record_id and Project.objects.filter(zoho_deal_id=record_id).exists():
        logger.info('Webhook: duplicate deal %s — skipped', record_id)
        return HttpResponse(status=200)

    # Field mapping
    account_name = (deal.get('Account_Name', '') or '').strip()
    contact_name = (deal.get('Contact_Name', '') or '').strip()
    if account_name:
        customer_name = account_name
        customer_contact_person = contact_name
    else:
        customer_name = contact_name
        customer_contact_person = ''

    raw_phone = str(deal.get('Mobile', '') or '').strip()
    digits_only = re.sub(r'\D', '', raw_phone)
    customer_phone = digits_only[-10:] if len(digits_only) >= 10 else digits_only

    raw_date = (deal.get('Closing_Date', '') or '').strip()
    target_commissioning_date = None
    if raw_date:
        try:
            target_commissioning_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except ValueError:
            pass

    pm_email = (deal.get('Assign_PM', '') or '').strip()
    assigned_pm = None
    if pm_email:
        profile_match = UserProfile.objects.filter(user__email__iexact=pm_email).first()
        if profile_match:
            assigned_pm = profile_match

    # Create project
    try:
        project = Project.objects.create(
            customer_name=customer_name or 'Unknown',
            customer_contact_person=customer_contact_person,
            customer_phone=customer_phone,
            customer_email=(deal.get('Customer_Email', '') or '').strip() or None,
            site_address='',
            city=(deal.get('City', '') or '').strip(),
            state=(deal.get('State', '') or '').strip() or 'Uttar Pradesh',
            project_type='Residential',
            capacity_kw=_safe_decimal(deal.get('Capacity_kW') or deal.get('Capacity')),
            contract_value=_safe_decimal(deal.get('Amount')),
            assigned_pm=assigned_pm,
            assigned_site_engineer=None,
            target_commissioning_date=target_commissioning_date,
            status='Draft',
            zoho_deal_id=record_id,
            created_by=None,
        )
    except Exception as exc:
        logger.error('Webhook: project creation failed for deal %s — %s', record_id, exc)
        # Return 200 even on failure — non-200 would cause Zoho to retry and create duplicates
        return HttpResponse(status=200)

    logger.info('Webhook: project %s created for deal %s (pm=%s)',
                project.project_id, record_id,
                project.assigned_pm.user.email if project.assigned_pm else 'unassigned')

    try:
        ActivityLog.objects.create(
            project=project,
            actor=None,
            action=f"Project '{project.project_id}' created via Zoho CRM webhook",
            entity_type='Project',
            entity_id=project.pk,
        )
    except Exception:
        pass

    if project.assigned_pm is None:
        # Notify Admin in-app: PM could not be assigned
        try:
            admin_profile = UserProfile.objects.filter(role='Admin').first()
            if admin_profile:
                send_notification(
                    recipient=admin_profile,
                    message=(
                        f'New Draft project {project.project_id} created from Zoho CRM '
                        f'(deal {record_id}) — PM could not be assigned. Please assign a PM.'
                    ),
                    channels=['in_app'],
                    link=f'/projects/{project.project_id}/overview/',
                    related_project=project,
                )
        except Exception as exc:
            logger.error('Webhook: in-app notification failed for project %s — %s', project.project_id, exc)
        # Also email a fixed admin address so the alert lands even if no Admin is logged in
        try:
            send_raw_email(
                to_email='smzk07@gmail.com',
                subject=f'[SolarPMS] Unassigned Project: {project.project_id}',
                body=(
                    f'A new Draft project was created via Zoho CRM but no PM could be assigned.\n\n'
                    f'Project ID : {project.project_id}\n'
                    f'Customer   : {project.customer_name}\n'
                    f'City       : {project.city or "—"}\n'
                    f'Zoho Deal  : {record_id}\n'
                    f'PM email from Zoho: {pm_email or "(blank)"}\n\n'
                    f'Please log in to the Admin Panel and assign a PM:\n'
                    f'https://horizon-solar-pms-production.up.railway.app/projects/{project.project_id}/overview/'
                ),
            )
        except Exception as exc:
            logger.error('Webhook: admin alert email failed for project %s — %s', project.project_id, exc)
    else:
        # Notify the assigned PM about their new project
        try:
            pm_display_name = project.assigned_pm.user.get_full_name() or project.assigned_pm.user.username
            project_link = reverse('project_overview', args=[project.pk])
            project_url_abs = request.build_absolute_uri(project_link)
            _ap_link = f'/projects/{project.project_id}/'
            _ap_message = (
                f'Hi {pm_display_name},\n\n'
                f'You have been assigned as Project Manager for {project.customer_name} '
                f'({project.city}).\n\n'
                f'Please review the project details and begin onboarding.'
            )
            _ap_email_message = (
                f'{_ap_message}\n\nView in Horizon Solar PMS:\n'
                f'https://horizon-solar-pms-production.up.railway.app{_ap_link}'
            )
            send_notification(
                recipient=project.assigned_pm,
                message=_ap_email_message,
                channels=['in_app', 'whatsapp', 'email'],
                link=_ap_link,
                subject=f'New Project Assigned: {project.customer_name}',
                template='assign_project',
                template_params=[
                    project.customer_name,    # [0] header
                    pm_display_name,          # [1] body[0] — user_name
                    project_url_abs,          # [2] body[1] — project_url
                ],
                related_project=project,
            )
        except Exception as exc:
            logger.error('Webhook: PM notification failed for project %s — %s', project.project_id, exc)

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# File upload helpers
# ---------------------------------------------------------------------------


def _validate_and_upload(file, supabase_client, bucket, supabase_path):
    """Validate one file and upload to Supabase. Raises ValueError on validation failure."""
    ext = file.name.rsplit('.', 1)[-1].lower() if '.' in file.name else ''
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported type (.{ext})")
    if file.size > MAX_FILE_SIZE_BYTES:
        raise ValueError("exceeds 20 MB limit")

    expected_mime = MIME_TYPE_MAP.get(ext, '')
    actual_mime   = (file.content_type or '').split(';')[0].strip()
    if expected_mime and actual_mime and actual_mime not in (expected_mime, 'application/octet-stream'):
        raise ValueError("MIME type does not match extension")

    file.seek(0)
    supabase_client.storage.from_(bucket).upload(
        path=supabase_path,
        file=file.read(),
        file_options={"content-type": MIME_TYPE_MAP.get(ext, 'application/octet-stream')},
    )
    return ext


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------

@login_required
def task_detail(request, project_id, task_id):
    """
    Task detail page: attachments, issues, and threaded comments for this task.
    Access: all roles; PM isolation applies.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # PM isolation: PM sees only their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    task        = get_object_or_404(Task, pk=task_id, phase__project=project)
    attachments = task.attachments.filter(is_deleted=False)
    task_issues = (
        Issue.objects.filter(task=task)
        .select_related('raised_by__user', 'assigned_to__user', 'task')
    )
    all_profiles = UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name')

    # Prefetch replies to avoid N+1 — template iterates comment.replies.all()
    task_comments = (
        Comment.objects.filter(task=task, parent=None)
        .select_related('author__user')
        .prefetch_related(
            Prefetch('replies', queryset=Comment.objects.select_related('author__user'))
        )
    )

    return render(request, 'projects/task_detail.html', {
        'project':       project,
        'task':          task,
        'attachments':   attachments,
        'user_profile':  profile,
        'task_issues':   task_issues,
        'all_profiles':  all_profiles,
        'task_comments': task_comments,
    })


# ---------------------------------------------------------------------------
# Project document upload / delete
# ---------------------------------------------------------------------------

@login_required
def upload_project_document(request, project_id):
    """
    Upload one or more files to a project. Files are stored in Supabase under
    project-documents/{project_id}/{uuid}_{filename}. DB record is created after
    a successful Supabase upload. Partial success (some files uploaded) shows a warning.
    Access: all roles with project access; PM isolation applies.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    files = request.FILES.getlist('files')
    if not files:
        messages.error(request, 'No files selected.')
        return redirect('project_overview', project_id=project_id)

    try:
        from .supabase_storage import get_supabase_client
        client = get_supabase_client()
    except (ValueError, ImportError) as exc:
        messages.error(request, f"Upload service unavailable. Contact Admin. ({exc})")
        return redirect('project_overview', project_id=project_id)

    bucket     = settings.SUPABASE_BUCKET
    successes  = []
    failures   = []

    for file in files:
        supabase_path = (
            f"project-documents/{project.project_id}/"
            f"{_uuid.uuid4()}_{file.name}"
        )
        try:
            ext       = _validate_and_upload(file, client, bucket, supabase_path)
            file_type = 'Photo' if ext in ALLOWED_PHOTO_EXTENSIONS else 'Document'
            file_url  = (
                f"{settings.SUPABASE_URL}/storage/v1/object/public/"
                f"{settings.SUPABASE_BUCKET}/{supabase_path}"
            )
            ProjectDocument.objects.create(
                project=project,
                uploaded_by=profile,
                file_name=file.name,
                file_url=file_url,
                supabase_path=supabase_path,
                file_type=file_type,
                file_size_kb=max(1, file.size // 1024),
            )
            successes.append(file.name)
        except ValueError as exc:
            failures.append(f"{file.name} ({exc})")
        except Exception as exc:
            logger.error('Supabase upload failed for %s: %s', file.name, exc)
            failures.append(f"{file.name} (upload error)")

    # Build outcome summary
    if successes and failures:
        msg   = (f"{len(successes)} of {len(successes)+len(failures)} files uploaded. "
                 f"Failed: {', '.join(failures)}")
        is_ok = True
        log_activity(project, profile, f"Uploaded {len(successes)} file(s) to project",
                     entity_type='File', entity_id=None)
    elif successes:
        msg   = f"{len(successes)} file(s) uploaded successfully."
        is_ok = True
        log_activity(project, profile, f"Uploaded {len(successes)} file(s) to project",
                     entity_type='File', entity_id=None)
    else:
        msg   = f"No files uploaded. Failed: {', '.join(failures)}"
        is_ok = False

    # AJAX request — return JSON so the browser can show inline feedback
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': is_ok, 'message': msg})

    # Regular POST fallback — Django messages + redirect
    if successes and failures:
        messages.warning(request, msg)
    elif successes:
        messages.success(request, msg)
    else:
        messages.error(request, msg)

    next_url = request.POST.get('next', '')
    if next_url and not _urlparse(next_url).netloc:
        return redirect(next_url)
    return redirect('project_overview', project_id=project_id)


@login_required
def delete_project_document(request, project_id, doc_pk):
    """
    Soft-delete a project document (sets is_deleted=True). The Supabase file is not
    removed here — purge_deleted_files handles hard deletion after FILE_RETENTION_DAYS.
    Access: uploader or Admin only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    doc = get_object_or_404(ProjectDocument, pk=doc_pk, project=project, is_deleted=False)

    if doc.uploaded_by != profile and profile.role != 'Admin':
        return HttpResponseForbidden()

    doc.is_deleted  = True
    doc.deleted_at  = timezone.now()
    doc.deleted_by  = profile
    doc.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])

    # Log the project document deletion
    log_activity(project, profile, f"Deleted file: {doc.file_name}", entity_type='File', entity_id=doc.pk)
    messages.success(request, f'"{doc.file_name}" deleted.')
    next_url = request.POST.get('next', '')
    if next_url and not _urlparse(next_url).netloc:
        return redirect(next_url)
    return redirect('project_overview', project_id=project_id)


# ---------------------------------------------------------------------------
# Task attachment upload / delete
# ---------------------------------------------------------------------------

@login_required
def upload_task_attachment(request, project_id, task_id):
    """
    Upload one or more files to a task. Stored in Supabase under
    task-attachments/{project_id}/{task_pk}/{uuid}_{filename}.
    Cross-project guard prevents uploading to a task on a different project.
    Access: all roles with project access; PM isolation applies.
    """
    if request.method != 'POST':
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    # Cross-project guard
    if task.phase.project.project_id != project_id:
        return HttpResponseForbidden()

    files = request.FILES.getlist('files')
    if not files:
        messages.error(request, 'No files selected.')
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    try:
        from .supabase_storage import get_supabase_client
        client = get_supabase_client()
    except (ValueError, ImportError) as exc:
        messages.error(request, f"Upload service unavailable. Contact Admin. ({exc})")
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    bucket    = settings.SUPABASE_BUCKET
    successes = []
    failures  = []

    for file in files:
        supabase_path = (
            f"task-attachments/{project.project_id}/{task.pk}/"
            f"{_uuid.uuid4()}_{file.name}"
        )
        try:
            ext       = _validate_and_upload(file, client, bucket, supabase_path)
            file_type = 'Photo' if ext in ALLOWED_PHOTO_EXTENSIONS else 'Document'
            file_url  = (
                f"{settings.SUPABASE_URL}/storage/v1/object/public/"
                f"{settings.SUPABASE_BUCKET}/{supabase_path}"
            )
            TaskAttachment.objects.create(
                task=task,
                uploaded_by=profile,
                file_name=file.name,
                file_url=file_url,
                supabase_path=supabase_path,
                file_type=file_type,
                file_size_kb=max(1, file.size // 1024),
            )
            successes.append(file.name)
        except ValueError as exc:
            failures.append(f"{file.name} ({exc})")
        except Exception as exc:
            logger.error('Supabase upload failed for %s: %s', file.name, exc)
            failures.append(f"{file.name} (upload error)")

    if successes and failures:
        messages.warning(
            request,
            f"{len(successes)} of {len(successes) + len(failures)} files uploaded. "
            f"Failed: {', '.join(failures)}"
        )
        # Log partial upload — at least some files were stored
        log_activity(project, profile, f"Uploaded {len(successes)} file(s) to task: {task.task_name}", entity_type='File', entity_id=task.pk)
    elif successes:
        messages.success(request, f"{len(successes)} file(s) uploaded successfully.")
        # Log successful task attachment upload
        log_activity(project, profile, f"Uploaded {len(successes)} file(s) to task: {task.task_name}", entity_type='File', entity_id=task.pk)
    else:
        messages.error(request, f"No files uploaded. Failed: {', '.join(failures)}")

    next_url = request.POST.get('next', '')
    if next_url and not _urlparse(next_url).netloc:
        return redirect(next_url)
    return redirect('task_detail', project_id=project_id, task_id=task_id)


@login_required
def delete_task_attachment(request, project_id, task_id, attach_pk):
    """
    Soft-delete a task attachment. Supabase file is purged later by purge_deleted_files.
    Access: uploader or Admin only. POST only.
    """
    if request.method != 'POST':
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    task   = get_object_or_404(Task, pk=task_id, phase__project=project)
    attach = get_object_or_404(TaskAttachment, pk=attach_pk, task=task, is_deleted=False)

    if attach.uploaded_by != profile and profile.role != 'Admin':
        return HttpResponseForbidden()

    attach.is_deleted = True
    attach.deleted_at = timezone.now()
    attach.deleted_by = profile
    attach.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])

    # Log the task attachment deletion
    log_activity(project, profile, f"Deleted file: {attach.file_name}", entity_type='File', entity_id=attach.pk)
    messages.success(request, f'"{attach.file_name}" deleted.')
    next_url = request.POST.get('next', '')
    if next_url and not _urlparse(next_url).netloc:
        return redirect(next_url)
    return redirect('task_detail', project_id=project_id, task_id=task_id)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def _issue_base_qs():
    """Base Issue queryset with actor relations pre-joined — used in issue detail views."""
    return Issue.objects.select_related('raised_by__user', 'assigned_to__user', 'task')


def _is_project_pm(profile, project):
    """Return True if profile is the PM assigned to project. Used for issue close/reopen guards."""
    return profile.role == 'PM' and project.assigned_pm == profile


@login_required
def create_project_issue(request, project_id):
    """
    Raise a project-level issue (not tied to any specific task).
    Access: all roles with project access; PM isolation applies. POST only.
    """
    if request.method != 'POST':
        return redirect('project_overview', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    title       = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    severity    = request.POST.get('severity', Issue.MEDIUM)
    due_date_s  = request.POST.get('due_date', '').strip()
    assignee_id = request.POST.get('assigned_to', '').strip()

    if not title:
        messages.error(request, 'Issue title is required.')
        return redirect('project_overview', project_id=project_id)

    if severity not in dict(Issue.SEVERITY_CHOICES):
        severity = Issue.MEDIUM

    due_date = None
    if due_date_s:
        try:
            due_date = date.fromisoformat(due_date_s)
        except ValueError:
            pass

    assigned_to = None
    if assignee_id:
        try:
            assigned_to = UserProfile.objects.get(pk=assignee_id)
        except UserProfile.DoesNotExist:
            pass

    issue = Issue.objects.create(
        project=project,
        task=None,
        title=title,
        description=description,
        severity=severity,
        status=Issue.OPEN,
        raised_by=profile,
        assigned_to=assigned_to,
        due_date=due_date,
    )
    log_activity(project, profile, f"Raised issue: {title} ({severity})", entity_type='Issue', entity_id=issue.pk)
    if assigned_to and assigned_to != profile:
        raiser_name = profile.user.get_full_name() or profile.user.username
        recipient_name = assigned_to.user.get_full_name() or assigned_to.user.username
        _ic_link = f'/issues/{issue.pk}/'
        _ic_message = (
            f'A new issue has been raised on project {project.customer_name}: '
            f'"{title}".\n\n'
            f'Please login to review and coordinate resolution.'
        )
        _ic_email_message = (
            f'{_ic_message}\n\nView in Horizon Solar PMS:\n'
            f'https://horizon-solar-pms-production.up.railway.app{_ic_link}'
        )
        send_notification(
            recipient=assigned_to,
            message=_ic_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=_ic_link,
            subject=f'New Issue Raised — {project.customer_name}',
            template='issue_created',
            template_params=[project.customer_name, recipient_name, project.customer_name, raiser_name],
            related_project=project,
            actor=profile,
        )
    if project.assigned_pm and project.assigned_pm != profile and project.assigned_pm != assigned_to:
        send_notification(
            recipient=project.assigned_pm,
            message=f'Issue "{title}" raised on {project.project_id} — {project.customer_name}.',
            channels=['in_app'],
            link=f'/issues/{issue.pk}/',
            related_project=project,
            actor=profile,
        )
    messages.success(request, f'Issue "{title}" raised successfully.')
    return redirect('project_overview', project_id=project_id)


@login_required
def create_task_issue(request, project_id, task_id):
    """
    Raise an issue linked to a specific task (task.issues relation).
    Access: all roles with project access; PM isolation applies. POST only.
    """
    if request.method != 'POST':
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    title       = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    severity    = request.POST.get('severity', Issue.MEDIUM)
    due_date_s  = request.POST.get('due_date', '').strip()
    assignee_id = request.POST.get('assigned_to', '').strip()

    if not title:
        messages.error(request, 'Issue title is required.')
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    if severity not in dict(Issue.SEVERITY_CHOICES):
        severity = Issue.MEDIUM

    due_date = None
    if due_date_s:
        try:
            due_date = date.fromisoformat(due_date_s)
        except ValueError:
            pass

    assigned_to = None
    if assignee_id:
        try:
            assigned_to = UserProfile.objects.get(pk=assignee_id)
        except UserProfile.DoesNotExist:
            pass

    issue = Issue.objects.create(
        project=project,
        task=task,
        title=title,
        description=description,
        severity=severity,
        status=Issue.OPEN,
        raised_by=profile,
        assigned_to=assigned_to,
        due_date=due_date,
    )
    log_activity(project, profile, f"Raised issue: {title} ({severity})", entity_type='Issue', entity_id=issue.pk)
    if assigned_to and assigned_to != profile:
        raiser_name = profile.user.get_full_name() or profile.user.username
        recipient_name = assigned_to.user.get_full_name() or assigned_to.user.username
        _ic_link = f'/issues/{issue.pk}/'
        _ic_message = (
            f'A new issue has been raised on project {project.customer_name}: '
            f'"{title}".\n\n'
            f'Please login to review and coordinate resolution.'
        )
        _ic_email_message = (
            f'{_ic_message}\n\nView in Horizon Solar PMS:\n'
            f'https://horizon-solar-pms-production.up.railway.app{_ic_link}'
        )
        send_notification(
            recipient=assigned_to,
            message=_ic_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=_ic_link,
            subject=f'New Issue Raised — {project.customer_name}',
            template='issue_created',
            template_params=[project.customer_name, recipient_name, project.customer_name, raiser_name],
            related_project=project,
            actor=profile,
        )
    if project.assigned_pm and project.assigned_pm != profile and project.assigned_pm != assigned_to:
        send_notification(
            recipient=project.assigned_pm,
            message=f'Issue "{title}" raised on {project.project_id} — {project.customer_name}.',
            channels=['in_app'],
            link=f'/issues/{issue.pk}/',
            related_project=project,
            actor=profile,
        )
    messages.success(request, f'Issue "{title}" raised.')
    return redirect('task_detail', project_id=project_id, task_id=task_id)


@login_required
def create_delivery_issue(request, project_id, dc_id):
    """
    Raise an issue linked to a specific DeliveryChallan (delivery_challan.issues relation).
    Follows the same pattern as create_task_issue but scoped to DC instead of Task.
    Access: all roles with project access; PM isolation applies. POST only.
    """
    if request.method != 'POST':
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # PM isolation: PMs can only interact with their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    # Cross-project guard: DC must belong to the project in the URL
    challan = get_object_or_404(DeliveryChallan, pk=dc_id)
    if challan.project.project_id != project_id:
        raise Http404

    title       = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    severity    = request.POST.get('severity', Issue.MEDIUM)
    due_date_s  = request.POST.get('due_date', '').strip()
    assignee_id = request.POST.get('assigned_to', '').strip()

    if not title:
        messages.error(request, 'Issue title is required.')
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    if severity not in dict(Issue.SEVERITY_CHOICES):
        severity = Issue.MEDIUM

    due_date = None
    if due_date_s:
        try:
            due_date = date.fromisoformat(due_date_s)
        except ValueError:
            pass

    assigned_to = None
    if assignee_id:
        try:
            assigned_to = UserProfile.objects.get(pk=assignee_id)
        except UserProfile.DoesNotExist:
            pass

    issue = Issue.objects.create(
        project=project,
        delivery_challan=challan,
        task=None,
        title=title,
        description=description,
        severity=severity,
        status=Issue.OPEN,
        raised_by=profile,
        assigned_to=assigned_to,
        due_date=due_date,
    )
    log_activity(
        project, profile,
        f"Raised delivery issue: {title} ({severity}) on DC {challan.dc_number}",
        entity_type='Issue', entity_id=issue.pk,
    )
    if assigned_to and assigned_to != profile:
        raiser_name = profile.user.get_full_name() or profile.user.username
        recipient_name = assigned_to.user.get_full_name() or assigned_to.user.username
        _ic_link = f'/issues/{issue.pk}/'
        _ic_message = (
            f'A new issue has been raised on project {project.customer_name}: '
            f'"{title}".\n\n'
            f'Please login to review and coordinate resolution.'
        )
        _ic_email_message = (
            f'{_ic_message}\n\nView in Horizon Solar PMS:\n'
            f'https://horizon-solar-pms-production.up.railway.app{_ic_link}'
        )
        send_notification(
            recipient=assigned_to,
            message=_ic_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=_ic_link,
            subject=f'New Issue Raised — {project.customer_name}',
            template='issue_created',
            template_params=[project.customer_name, recipient_name, project.customer_name, raiser_name],
            related_project=project,
            actor=profile,
        )
    if project.assigned_pm and project.assigned_pm != profile and project.assigned_pm != assigned_to:
        send_notification(
            recipient=project.assigned_pm,
            message=f'Issue "{title}" raised on {project.project_id} — {project.customer_name}.',
            channels=['in_app'],
            link=f'/issues/{issue.pk}/',
            related_project=project,
            actor=profile,
        )
    messages.success(request, f'Issue "{title}" raised for DC {challan.dc_number}.')
    return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)


@login_required
def issue_detail(request, issue_id):
    """
    Issue detail page with comments and status/assignee controls.
    Access: all roles; PM isolation applies.
    """
    issue   = get_object_or_404(_issue_base_qs(), pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    all_profiles = UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name')
    is_pm        = _is_project_pm(profile, project)

    # Prefetch replies to avoid N+1 — template iterates comment.replies.all()
    issue_comments = (
        Comment.objects.filter(issue=issue, parent=None)
        .select_related('author__user')
        .prefetch_related(
            Prefetch('replies', queryset=Comment.objects.select_related('author__user'))
        )
    )

    return render(request, 'projects/issue_detail.html', {
        'issue':          issue,
        'project':        project,
        'user_profile':   profile,
        'all_profiles':   all_profiles,
        'is_pm':          is_pm,
        'issue_comments': issue_comments,
    })


@login_required
def update_issue_status(request, issue_id):
    """
    Advance an Open issue to In Progress. Requires the issue to have an assignee.
    filter().update() used to prevent race condition on concurrent status changes.
    Access: all roles with project access. POST only.
    """
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    if issue.status == Issue.CLOSED:
        return HttpResponseForbidden('Issue is closed.')

    if issue.status != Issue.OPEN:
        messages.warning(request, 'Issue is not in Open status.')
        return redirect('issue_detail', issue_id=issue_id)

    if issue.assigned_to is None:
        messages.warning(request, 'Please assign this issue before starting work.')
        return redirect('issue_detail', issue_id=issue_id)

    # filter().update() with status condition prevents race condition — updated==0 means
    # another request already changed the status between our read and this write
    updated = Issue.objects.filter(pk=issue.pk, status=Issue.OPEN).update(status=Issue.IN_PROGRESS)
    if updated == 0:
        messages.warning(request, 'Issue status was already updated.')
    else:
        log_activity(project, profile, f"Moved issue to In Progress: {issue.title}", entity_type='Issue', entity_id=issue.pk)
        messages.success(request, 'Issue moved to In Progress.')
    return redirect('issue_detail', issue_id=issue_id)


@login_required
def resolve_issue(request, issue_id):
    """
    Mark an In Progress issue as Resolved. Resolution note is required.
    Notifies the PM (if they are not the resolver) so they can review and close.
    filter().update() used to prevent race condition on concurrent status changes.
    Access: any role with project access. POST only.
    """
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    if issue.status == Issue.CLOSED:
        return HttpResponseForbidden('Issue is closed.')

    if issue.status != Issue.IN_PROGRESS:
        messages.warning(request, 'Issue must be In Progress to resolve.')
        return redirect('issue_detail', issue_id=issue_id)

    resolution_note = request.POST.get('resolution_note', '').strip()
    if not resolution_note:
        messages.error(request, 'Resolution note is required.')
        return redirect('issue_detail', issue_id=issue_id)

    updated = Issue.objects.filter(pk=issue.pk, status=Issue.IN_PROGRESS).update(
        status=Issue.RESOLVED,
        resolved_at=timezone.now(),
        resolution_note=resolution_note,
    )
    if updated == 0:
        messages.warning(request, 'Issue status was already updated.')
    else:
        log_activity(project, profile, f"Resolved issue: {issue.title}", entity_type='Issue', entity_id=issue.pk)
        resolver_name = profile.user.get_full_name() or profile.user.username
        issue_link = f'/issues/{issue.pk}/'
        issue_link_abs = request.build_absolute_uri(issue_link)
        resolved_params = [project.customer_name, issue.title, resolver_name, issue_link_abs]
        _ir_message = (
            f'The issue "{issue.title}" on project {project.customer_name} '
            f'has been marked as resolved by {resolver_name}.'
        )
        _ir_email_message = (
            f'{_ir_message}\n\nView in Horizon Solar PMS:\n'
            f'https://horizon-solar-pms-production.up.railway.app{issue_link}'
        )
        notified_pks = {profile.pk}
        for notify_recipient in [project.assigned_pm, issue.assigned_to, issue.raised_by]:
            if notify_recipient and notify_recipient.pk not in notified_pks:
                notified_pks.add(notify_recipient.pk)
                send_notification(
                    recipient=notify_recipient,
                    message=_ir_email_message,
                    channels=['in_app', 'whatsapp', 'email'],
                    link=issue_link,
                    subject=f'Issue Resolved — {project.customer_name}',
                    template='issue_resolved',
                    template_params=resolved_params,
                    related_project=project,
                    actor=profile,
                )
        messages.success(request, 'Issue marked as Resolved. PM has been notified.')
    return redirect('issue_detail', issue_id=issue_id)


@login_required
def close_issue(request, issue_id):
    """
    Close a Resolved issue. Only the project PM can close.
    filter().update() used to prevent race condition on concurrent status changes.
    Access: project PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if not _is_project_pm(profile, project):
        return HttpResponseForbidden('Only the project PM can close issues.')

    if issue.status != Issue.RESOLVED:
        messages.warning(request, 'Issue must be Resolved before it can be closed.')
        return redirect('issue_detail', issue_id=issue_id)

    updated = Issue.objects.filter(pk=issue.pk, status=Issue.RESOLVED).update(
        status=Issue.CLOSED,
        closed_at=timezone.now(),
    )
    if updated == 0:
        messages.warning(request, 'Issue status was already updated.')
    else:
        log_activity(project, profile, f"PM closed issue: {issue.title}", entity_type='Issue', entity_id=issue.pk)
        messages.success(request, 'Issue closed.')
    return redirect('issue_detail', issue_id=issue_id)


@login_required
def reopen_issue(request, issue_id):
    """
    Reopen a Resolved issue back to Open (clears resolved_at and resolution_note).
    Only the project PM can reopen. filter().update() prevents race conditions.
    Access: project PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if not _is_project_pm(profile, project):
        return HttpResponseForbidden('Only the project PM can reopen issues.')

    if issue.status != Issue.RESOLVED:
        messages.warning(request, 'Only Resolved issues can be reopened.')
        return redirect('issue_detail', issue_id=issue_id)

    updated = Issue.objects.filter(pk=issue.pk, status=Issue.RESOLVED).update(
        status=Issue.OPEN,
        resolved_at=None,
        resolution_note='',
    )
    if updated == 0:
        messages.warning(request, 'Issue status was already updated.')
    else:
        log_activity(project, profile, f"PM reopened issue: {issue.title}", entity_type='Issue', entity_id=issue.pk)
        messages.success(request, 'Issue reopened.')
    return redirect('issue_detail', issue_id=issue_id)


@login_required
def assign_issue(request, issue_id):
    """
    Assign or unassign an issue. Closed issues cannot be reassigned.
    filter().update() used for atomic update without loading the full object.
    Access: all roles with project access. POST only.
    """
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    if issue.status == Issue.CLOSED:
        return HttpResponseForbidden('Cannot reassign a closed issue.')

    assignee_id = request.POST.get('assigned_to', '').strip()
    if assignee_id:
        try:
            new_assignee = UserProfile.objects.get(pk=assignee_id)
        except UserProfile.DoesNotExist:
            messages.error(request, 'Invalid user selected.')
            return redirect('issue_detail', issue_id=issue_id)
        Issue.objects.filter(pk=issue.pk).update(assigned_to=new_assignee)
        log_activity(
            project, profile,
            f"Assigned issue to {new_assignee.user.get_full_name() or new_assignee.user.username}",
            entity_type='Issue', entity_id=issue.pk,
        )
        messages.success(request, 'Issue assigned.')
    else:
        Issue.objects.filter(pk=issue.pk).update(assigned_to=None)
        log_activity(project, profile, f"Unassigned issue: {issue.title}", entity_type='Issue', entity_id=issue.pk)
        messages.success(request, 'Issue unassigned.')
    return redirect('issue_detail', issue_id=issue_id)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@login_required
def create_task_comment(request, project_id, task_id):
    """Create a top-level comment or reply on a task. All roles. POST only."""
    if request.method != 'POST':
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    project = get_object_or_404(Project, project_id=project_id)
    task    = get_object_or_404(Task, pk=task_id, phase__project=project)
    profile = request.user.profile

    # PM isolation: PM can only access their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    body = request.POST.get('body', '').strip()
    if not body:
        messages.error(request, 'Comment body is required.')
        return redirect('task_detail', project_id=project_id, task_id=task_id)

    parent_pk      = request.POST.get('parent', '').strip()
    parent_comment = None

    if parent_pk:
        parent_comment = get_object_or_404(Comment, pk=parent_pk)
        # Reject replies to replies — only one level of nesting supported
        if parent_comment.parent is not None:
            return HttpResponse("Replies to replies are not supported.", status=400)
        # Parent must belong to this same task, not another
        if parent_comment.task != task:
            return HttpResponse("Parent comment mismatch.", status=400)

    Comment.objects.create(
        project=project,
        task=task,
        issue=None,
        parent=parent_comment,
        author=profile,
        body=body,
    )

    # log_activity() is wrapped in try/except internally —
    # a failed log must never block the primary action
    if parent_comment:
        log_activity(project, profile, f"Replied to comment on task: {task.task_name}", entity_type='Comment', entity_id=task.pk)
    else:
        log_activity(project, profile, f"Commented on task: {task.task_name}", entity_type='Comment', entity_id=task.pk)

    return redirect('task_detail', project_id=project_id, task_id=task_id)


@login_required
def create_issue_comment(request, issue_id):
    """Create a top-level comment or reply on an issue. All roles. POST only."""
    if request.method != 'POST':
        return redirect('issue_detail', issue_id=issue_id)

    issue   = get_object_or_404(Issue, pk=issue_id)
    project = issue.project
    profile = request.user.profile

    # PM isolation: PM can only access their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    body = request.POST.get('body', '').strip()
    if not body:
        messages.error(request, 'Comment body is required.')
        return redirect('issue_detail', issue_id=issue_id)

    parent_pk      = request.POST.get('parent', '').strip()
    parent_comment = None

    if parent_pk:
        parent_comment = get_object_or_404(Comment, pk=parent_pk)
        # Reject replies to replies — only one level of nesting supported
        if parent_comment.parent is not None:
            return HttpResponse("Replies to replies are not supported.", status=400)
        # Parent must belong to this same issue, not another
        if parent_comment.issue != issue:
            return HttpResponse("Parent comment mismatch.", status=400)

    Comment.objects.create(
        project=project,
        task=None,
        issue=issue,
        parent=parent_comment,
        author=profile,
        body=body,
    )

    # log_activity() is wrapped in try/except internally —
    # a failed log must never block the primary action
    if parent_comment:
        log_activity(project, profile, f"Replied to comment on issue: {issue.title}", entity_type='Comment', entity_id=issue.pk)
    else:
        log_activity(project, profile, f"Commented on issue: {issue.title}", entity_type='Comment', entity_id=issue.pk)

    return redirect('issue_detail', issue_id=issue_id)


@login_required
def delete_comment(request, comment_id):
    """Soft-delete a comment. Author or Admin only. POST only."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    comment = get_object_or_404(Comment, pk=comment_id)
    profile = request.user.profile

    # Only author or Admin can delete — other roles get 403
    if comment.author != profile and profile.role != 'Admin':
        return HttpResponse(status=403)

    comment.is_deleted = True
    comment.deleted_at = timezone.now()
    comment.save(update_fields=['is_deleted', 'deleted_at'])

    # Log then redirect to the originating detail page
    if comment.task:
        log_activity(
            comment.project, profile,
            f"Deleted comment on task: {comment.task.task_name}",
            entity_type='Comment', entity_id=comment.task.pk,
        )
        return redirect('task_detail', project_id=comment.project.project_id, task_id=comment.task.pk)
    else:
        log_activity(
            comment.project, profile,
            f"Deleted comment on issue: {comment.issue.title}",
            entity_type='Comment', entity_id=comment.issue.pk,
        )
        return redirect('issue_detail', issue_id=comment.issue.pk)


# ---------------------------------------------------------------------------
# Activity log views
# ---------------------------------------------------------------------------

@login_required
def project_timeline(request, project_id):
    """Project activity timeline. All roles — PM isolation applies."""
    from django.core.paginator import Paginator

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # PM isolation: PM can only view their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    # All activity logs for this project, newest first, with actor data
    logs = (
        project.activity_logs
        .select_related('actor__user')
        .order_by('-timestamp')
    )

    paginator   = Paginator(logs, 20)
    page_number = request.GET.get('page')
    page_obj    = paginator.get_page(page_number)

    return render(request, 'projects/project_timeline.html', {
        'project':  project,
        'page_obj': page_obj,
    })


@login_required
@role_required(['Admin'])
def portal_activity_log(request):
    """Cross-project activity log with filters. Admin only."""
    from django.core.paginator import Paginator

    # All logs across all projects, newest first
    logs = ActivityLog.objects.select_related(
        'project', 'actor__user'
    ).order_by('-timestamp')

    # Conditional filter values — all optional, empty = show all
    project_id  = request.GET.get('project_id',  '').strip()
    actor_id    = request.GET.get('actor_id',    '').strip()
    entity_type = request.GET.get('entity_type', '').strip()
    date_from   = request.GET.get('date_from',   '').strip()
    date_to     = request.GET.get('date_to',     '').strip()

    if project_id:
        logs = logs.filter(project__project_id=project_id)
    if actor_id:
        logs = logs.filter(actor__pk=actor_id)
    if entity_type:
        logs = logs.filter(entity_type=entity_type)
    if date_from:
        try:
            logs = logs.filter(timestamp__date__gte=date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            logs = logs.filter(timestamp__date__lte=date.fromisoformat(date_to))
        except ValueError:
            pass

    paginator   = Paginator(logs, 50)
    page_number = request.GET.get('page')
    page_obj    = paginator.get_page(page_number)

    # Actor dropdown: only users who have actually logged actions
    actors = (
        UserProfile.objects
        .filter(activity_logs__isnull=False)
        .distinct()
        .select_related('user')
        .order_by('user__first_name')
    )
    all_projects = Project.objects.order_by('project_id')
    # Hardcoded entity type list matches what views log
    entity_types = ['Issue', 'Comment', 'Task', 'BOQ', 'Milestone', 'File']

    return render(request, 'projects/portal_activity_log.html', {
        'page_obj':     page_obj,
        'all_projects': all_projects,
        'actors':       actors,
        'entity_types': entity_types,
        'filters': {
            'project_id':  project_id,
            'actor_id':    actor_id,
            'entity_type': entity_type,
            'date_from':   date_from,
            'date_to':     date_to,
        },
    })


@login_required
@role_required(['Admin'])
def admin_whatsapp_log(request):
    """WhatsApp diagnostic log — API send status + Interakt delivery status. Admin only."""
    from django.core.paginator import Paginator

    status_param     = request.GET.get('status',     '').strip()
    project_id_param = request.GET.get('project_id', '').strip()
    date_from_param  = request.GET.get('date_from',  '').strip()
    date_to_param    = request.GET.get('date_to',    '').strip()

    default_date_from = (timezone.now().date() - timedelta(days=7)).isoformat()
    date_from = date_from_param or default_date_from

    qs = (
        NotificationLog.objects
        .filter(channel='whatsapp')
        .select_related('recipient__user', 'related_project', 'actor__user')
        .order_by('-created_at')
    )

    if status_param:
        qs = qs.filter(status=status_param)
    if project_id_param:
        qs = qs.filter(related_project_id=project_id_param)
    if date_from:
        try:
            qs = qs.filter(created_at__date__gte=date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to_param:
        try:
            qs = qs.filter(created_at__date__lte=date.fromisoformat(date_to_param))
        except ValueError:
            pass

    sent_count            = qs.filter(status='sent').count()
    failed_count          = qs.filter(status='failed').count()
    skipped_count         = qs.filter(status='skipped').count()
    delivered_count       = qs.filter(delivery_status='message_api_delivered').count()
    read_count            = qs.filter(delivery_status='message_api_read').count()
    delivery_failed_count = qs.filter(delivery_status='message_api_failed').count()
    pending_count         = qs.filter(status='sent', delivery_status='').count()

    project_list = Project.objects.filter(is_deleted=False).order_by('customer_name')

    paginator   = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj    = paginator.get_page(page_number)

    return render(request, 'projects/admin_whatsapp_log.html', {
        'page_obj':             page_obj,
        'sent_count':           sent_count,
        'failed_count':         failed_count,
        'skipped_count':        skipped_count,
        'delivered_count':      delivered_count,
        'read_count':           read_count,
        'delivery_failed_count': delivery_failed_count,
        'pending_count':        pending_count,
        'project_list':         project_list,
        'current_filters': {
            'status':     status_param,
            'project_id': project_id_param,
            'date_from':  date_from,
            'date_to':    date_to_param,
        },
    })


# ---------------------------------------------------------------------------
# Delivery Challans (SCM Delivery Tracker — Day 9)
# ---------------------------------------------------------------------------

@login_required
@role_required(['SCM'])
def create_delivery_challan(request, project_id):
    """
    Create a new Delivery Challan for a project. Access: SCM only.
    Dynamic line items are submitted with indexed field names (line_item_*_N).
    Minimum 1 line item required — zero items rejected with a validation error.
    GET renders the form; POST creates the DC + line items in one transaction.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile
    vendors = Vendor.objects.filter(is_active=True).order_by('name')

    if request.method != 'POST':
        return render(request, 'projects/delivery_challan_create.html', {
            'project':          project,
            'vendors':          vendors,
            'category_choices': DCLineItem.CATEGORY_CHOICES,
        })

    # Parse DC header fields
    vendor_id              = request.POST.get('vendor_id', '').strip()
    po_number              = request.POST.get('po_number', '').strip()
    dc_number              = request.POST.get('dc_number', '').strip()
    dc_date_s              = request.POST.get('dc_date', '').strip()
    expected_delivery_s    = request.POST.get('expected_delivery_date', '').strip()
    notes                  = request.POST.get('notes', '').strip()

    if not dc_number or not dc_date_s:
        messages.error(request, 'DC Number and DC Date are required.')
        return render(request, 'projects/delivery_challan_create.html', {
            'project': project, 'vendors': vendors,
            'category_choices': DCLineItem.CATEGORY_CHOICES,
        })

    try:
        dc_date = date.fromisoformat(dc_date_s)
    except ValueError:
        messages.error(request, 'Invalid DC Date format.')
        return render(request, 'projects/delivery_challan_create.html', {
            'project': project, 'vendors': vendors,
            'category_choices': DCLineItem.CATEGORY_CHOICES,
        })

    expected_delivery_date = None
    if expected_delivery_s:
        try:
            expected_delivery_date = date.fromisoformat(expected_delivery_s)
        except ValueError:
            pass

    vendor = None
    if vendor_id:
        try:
            vendor = Vendor.objects.get(pk=vendor_id, is_active=True)
        except Vendor.DoesNotExist:
            pass

    # Parse dynamically indexed line item fields from POST
    # Field names follow the pattern: line_item_category_N, line_item_description_N, etc.
    line_items_data = []
    i = 0
    while f'line_item_category_{i}' in request.POST:
        category    = request.POST.get(f'line_item_category_{i}', '').strip()
        description = request.POST.get(f'line_item_description_{i}', '').strip()
        qty_str     = request.POST.get(f'line_item_qty_{i}', '').strip()
        unit        = request.POST.get(f'line_item_unit_{i}', 'Nos').strip() or 'Nos'
        qty         = _safe_decimal(qty_str)
        if category and description and qty:
            line_items_data.append({
                'boq_category':     category,
                'item_description': description,
                'ordered_quantity': qty,
                'unit':             unit,
            })
        i += 1

    # Minimum 1 line item — DC with zero items rejected
    if not line_items_data:
        messages.error(request, 'At least one line item is required.')
        return render(request, 'projects/delivery_challan_create.html', {
            'project': project, 'vendors': vendors,
            'category_choices': DCLineItem.CATEGORY_CHOICES,
        })

    with transaction.atomic():
        challan = DeliveryChallan.objects.create(
            project=project,
            vendor=vendor,
            po_number=po_number,
            dc_number=dc_number,
            dc_date=dc_date,
            expected_delivery_date=expected_delivery_date,
            status=DeliveryChallan.EXPECTED,
            notes=notes,
            created_by=profile,
        )
        # Create line items after challan exists; recalculate_dc_status is NOT called
        # here because all new items have no received_quantity → status stays Expected
        for item_data in line_items_data:
            DCLineItem.objects.create(challan=challan, **item_data)

    vendor_name = vendor.name if vendor else 'Unknown Vendor'
    log_activity(
        project, profile,
        f"SCM created Delivery Challan {dc_number} for {vendor_name}",
        entity_type='DeliveryChallan', entity_id=challan.pk,
    )
    messages.success(request, f'Delivery Challan {dc_number} created successfully.')
    return redirect('delivery_challan_detail', project_id=project_id, dc_id=challan.pk)


@login_required
def delivery_challan_detail(request, project_id, dc_id):
    """
    DC detail page: line items table, GRN confirmation form (SE), Edit GRN button (SCM).
    Cross-project guard: DC must belong to the project in the URL — returns 404 on mismatch
    to prevent cross-project data leakage via URL manipulation.
    Access: SCM, PM, SE, Admin.
    """
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # Only SCM, PM, SE, Admin can view DC pages
    if profile.role not in ('SCM', 'PM', 'Site Engineer', 'Admin'):
        return HttpResponseForbidden()

    # PM isolation: PM sees only their own projects
    if profile.role == 'PM' and project.assigned_pm != profile:
        raise Http404

    # Cross-project guard: DC must belong to the project in the URL
    challan    = get_object_or_404(DeliveryChallan, pk=dc_id)
    if challan.project.project_id != project_id:
        raise Http404

    line_items = challan.line_items.select_related('grn_confirmed_by__user').all()

    # Issues raised against this specific DC — shown below line items for visibility
    dc_issues = (
        Issue.objects.filter(delivery_challan=challan)
        .select_related('raised_by__user', 'assigned_to__user')
        .order_by('-raised_at')
    )

    return render(request, 'projects/delivery_challan_detail.html', {
        'project':           project,
        'challan':           challan,
        'line_items':        line_items,
        'dc_issues':         dc_issues,
        'role':              profile.role,
        'user_profile':      profile,
        'condition_choices': DCLineItem.CONDITION_CHOICES,
        'all_profiles':      UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
@role_required(['Site Engineer'])
def confirm_grn(request, project_id, dc_id):
    """
    SE confirms receipt of materials at site (GRN confirmation).
    Captures received_quantity and damaged_quantity separately — two numeric inputs
    give precise information rather than a coarse condition dropdown.
    condition field is derived for backward compatibility with code that reads it.
    recalculate_dc_status() called ONCE after all line items saved — never inside the loop.
    Access: Site Engineer only. POST only.
    """
    if request.method != 'POST':
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # Cross-project guard
    challan = get_object_or_404(DeliveryChallan, pk=dc_id)
    if challan.project.project_id != project_id:
        raise Http404

    # SE cannot confirm an already-Received DC — button is hidden in UI but guard here too
    if challan.status == DeliveryChallan.RECEIVED:
        return HttpResponseForbidden()

    today      = date.today()
    line_items = challan.line_items.all()

    for item in line_items:
        qty_str        = request.POST.get(f'received_qty_{item.pk}', '').strip()
        damaged_qty_str = request.POST.get(f'damaged_qty_{item.pk}', '0').strip()
        grn_notes      = request.POST.get(f'grn_notes_{item.pk}', '').strip()

        if not qty_str:
            continue  # Skip items where SE didn't enter a received quantity

        received_qty = _safe_decimal(qty_str)
        if received_qty is None:
            continue

        # Parse damaged_quantity; clamp to [0, received_qty]
        try:
            damaged_qty = max(0, int(float(damaged_qty_str)))
        except (ValueError, TypeError):
            damaged_qty = 0
        damaged_qty = min(damaged_qty, int(received_qty))

        # Derive condition for backward compatibility with code that reads this field
        # condition string is now secondary — damaged_quantity is the precise source of truth
        if damaged_qty == 0:
            derived_condition = DCLineItem.GOOD
        elif damaged_qty >= received_qty:
            derived_condition = DCLineItem.DAMAGED
        else:
            derived_condition = DCLineItem.PARTIAL

        item.received_quantity = received_qty
        item.damaged_quantity  = damaged_qty
        item.condition         = derived_condition
        item.grn_date          = today
        item.grn_confirmed_by  = profile
        item.grn_notes         = grn_notes
        item.save()

    # Recalculate DC status ONCE after all line items saved — never inside the loop
    # (calling it inside the loop causes status to oscillate incorrectly)
    recalculate_dc_status(challan)
    challan.refresh_from_db()

    log_activity(
        project, profile,
        f"SE confirmed GRN for DC {challan.dc_number} — {challan.status}",
        entity_type='DeliveryChallan', entity_id=challan.pk,
    )
    messages.success(request, f'GRN confirmed. DC status: {challan.status}.')
    return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)


@login_required
@role_required(['SCM'])
def override_grn(request, project_id, dc_id):
    """
    SCM overrides an SE-submitted GRN. grn_confirmed_by is NOT overwritten —
    original SE submitter is preserved. ActivityLog records who made the override.
    No status restriction: SCM can override even on Received/Rejected DCs (to correct mistakes).
    Captures received_quantity and damaged_quantity separately — mirrors confirm_grn logic.
    recalculate_dc_status() called ONCE after all items saved.
    Access: SCM only. POST only.
    """
    if request.method != 'POST':
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    # Cross-project guard
    challan = get_object_or_404(DeliveryChallan, pk=dc_id)
    if challan.project.project_id != project_id:
        raise Http404

    today      = date.today()
    line_items = challan.line_items.all()

    for item in line_items:
        qty_str        = request.POST.get(f'received_qty_{item.pk}', '').strip()
        damaged_qty_str = request.POST.get(f'damaged_qty_{item.pk}', '0').strip()
        grn_notes      = request.POST.get(f'grn_notes_{item.pk}', '').strip()

        if not qty_str:
            continue  # Skip items where SCM didn't enter a received quantity

        received_qty = _safe_decimal(qty_str)
        if received_qty is None:
            continue

        # Parse damaged_quantity; clamp to [0, received_qty]
        try:
            damaged_qty = max(0, int(float(damaged_qty_str)))
        except (ValueError, TypeError):
            damaged_qty = 0
        damaged_qty = min(damaged_qty, int(received_qty))

        # Derive condition for backward compatibility
        if damaged_qty == 0:
            derived_condition = DCLineItem.GOOD
        elif damaged_qty >= received_qty:
            derived_condition = DCLineItem.DAMAGED
        else:
            derived_condition = DCLineItem.PARTIAL

        item.received_quantity = received_qty
        item.damaged_quantity  = damaged_qty
        item.condition         = derived_condition
        item.grn_date          = today
        item.grn_notes         = grn_notes
        # grn_confirmed_by NOT overwritten — original SE submitter is preserved
        item.save()

    # Recalculate DC status ONCE after all line items saved
    recalculate_dc_status(challan)
    challan.refresh_from_db()

    log_activity(
        project, profile,
        f"SCM overrode GRN for DC {challan.dc_number} — override by {profile.user.get_full_name() or profile.user.username}",
        entity_type='DeliveryChallan', entity_id=challan.pk,
    )
    messages.success(request, f'GRN overridden. DC status: {challan.status}.')
    return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)


# ---------------------------------------------------------------------------
# Interakt Delivery Webhook — publicly reachable, no auth required
# ---------------------------------------------------------------------------

INTERAKT_DELIVERY_PRIORITY = {
    'message_api_read':      4,
    'message_api_delivered': 3,
    'message_api_sent':      2,
    'message_api_failed':    1,
    '':                      0,
}

INTERAKT_VALID_DELIVERY_TYPES = frozenset(INTERAKT_DELIVERY_PRIORITY) - {''}


@csrf_exempt
def interakt_webhook(request):
    import hmac as _hmac
    import hashlib
    import os

    if request.method != 'POST':
        return HttpResponse(status=405)

    secret = os.environ.get('INTERAKT_WEBHOOK_SECRET', '')
    if secret:
        signature = request.headers.get('Interakt-Signature', '')
        expected = 'sha256=' + _hmac.new(
            secret.encode('utf-8'),
            request.body,
            hashlib.sha256,
        ).hexdigest()
        if not _hmac.compare_digest(signature, expected):
            logger.warning(
                'Interakt webhook signature mismatch. Got: %s', signature
            )
            return HttpResponse(status=200)

    try:
        payload = json.loads(request.body)
    except Exception:
        return HttpResponse(status=200)

    event_type = payload.get('type', '')
    message_id = payload.get('data', {}).get('message', {}).get('id', '')

    if event_type not in INTERAKT_VALID_DELIVERY_TYPES or not message_id:
        return HttpResponse(status=200)

    try:
        log = NotificationLog.objects.filter(interakt_message_id=message_id).first()
        if log:
            current_priority = INTERAKT_DELIVERY_PRIORITY.get(log.delivery_status, 0)
            new_priority      = INTERAKT_DELIVERY_PRIORITY.get(event_type, 0)
            if new_priority > current_priority:
                log.delivery_status = event_type
                log.save(update_fields=['delivery_status'])
    except Exception as e:
        logger.error('Interakt webhook DB update failed: %s', e)

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# My Documents — personal record-keeping page
# ---------------------------------------------------------------------------

@login_required
def my_documents(request):
    """Personal document archive for the logged-in user. Role-aware sections."""
    profile = request.user.profile
    role    = profile.role

    # Section A — Uploaded Files (all roles)
    task_attachments = TaskAttachment.objects.filter(
        uploaded_by=profile,
        is_deleted=False,
    ).select_related('task', 'task__phase', 'task__phase__project').order_by('-uploaded_at')[:50]

    project_docs = ProjectDocument.objects.filter(
        uploaded_by=profile,
        is_deleted=False,
    ).select_related('project').order_by('-uploaded_at')[:50]

    # Section B — BOQ Submissions (Design only)
    boq_list = []
    if role == 'Design':
        boq_list = BOQ.objects.filter(
            submitted_by=profile,
        ).select_related('project').order_by('-submitted_at')[:50]

    # Section C — Design Submissions (Design only)
    design_list = []
    if role == 'Design':
        design_list = DesignSubmission.objects.filter(
            submitted_by=profile,
        ).select_related('project').order_by('-submitted_at')[:50]

    # Section D — Delivery Challans (SCM only)
    dc_list = []
    if role == 'SCM':
        dc_list = DeliveryChallan.objects.filter(
            created_by=profile,
        ).select_related('project').order_by('-created_at')[:50]

    # Section E — Payment Requests (SCM only)
    # requested_by is FK to auth.User (not UserProfile)
    pr_list = []
    if role == 'SCM':
        pr_list = PaymentRequest.objects.filter(
            requested_by=request.user,
        ).select_related('project', 'vendor').order_by('-requested_date')[:50]

    context = {
        'task_attachments': task_attachments,
        'project_docs':     project_docs,
        'boq_list':         boq_list,
        'design_list':      design_list,
        'dc_list':          dc_list,
        'pr_list':          pr_list,
        'role':             role,
    }
    return render(request, 'projects/my_documents.html', context)


@login_required
def design_submission_detail(request, pk):
    """Read-only detail view for a DesignSubmission. Submitter, PM, or Admin only."""
    submission = get_object_or_404(DesignSubmission, pk=pk)
    profile    = request.user.profile
    if profile != submission.submitted_by and profile.role not in ('PM', 'Admin'):
        messages.error(request, "You don't have access to this submission.")
        return redirect('my_documents')
    return render(request, 'projects/design_submission_detail.html', {'submission': submission})


@login_required
def payment_request_detail(request, project_id, request_id):
    """Read-only detail view for a PaymentRequest. SCM, Finance, PM, or Admin only."""
    project = get_object_or_404(Project, project_id=project_id)
    pr      = get_object_or_404(PaymentRequest, pk=request_id, project=project)
    profile = request.user.profile
    if profile.role not in ('SCM', 'Finance', 'PM', 'Admin'):
        messages.error(request, "You don't have access to this payment request.")
        return redirect('my_documents')
    return render(request, 'projects/payment_request_detail.html', {
        'pr':      pr,
        'project': project,
    })


# ---------------------------------------------------------------------------
# Admin Panel (portal-admin/) — Admin role only
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def admin_master_switches(request):
    """Screen 1: Master switches for WhatsApp, email, in-app notifications, maintenance mode, cascade scheduling."""
    settings = SystemSettings.get()

    FIELD_LABELS = {
        'whatsapp_enabled':             'WhatsApp notifications (Interakt)',
        'email_enabled':                'Email notifications (ZeptoMail)',
        'in_app_notifications_enabled': 'In-app notifications',
        'maintenance_mode':             'Maintenance mode',
    }

    if request.method == 'POST':
        actor = request.user.profile
        for field, label in FIELD_LABELS.items():
            old_value = getattr(settings, field)
            new_value = field in request.POST
            if old_value != new_value:
                setattr(settings, field, new_value)
                log_activity(
                    project=None,
                    actor=actor,
                    action=f"Master switch '{label}' set to {'ON' if new_value else 'OFF'}",
                    entity_type='System',
                    entity_id=None,
                )
        # Cascade scheduling feature gate — handled separately for specific log message
        old_cascade = settings.cascade_scheduling_enabled
        new_cascade = 'cascade_scheduling_enabled' in request.POST
        if old_cascade != new_cascade:
            settings.cascade_scheduling_enabled = new_cascade
            log_activity(
                project=None,
                actor=actor,
                action=f"Cascading scheduling feature gate set to {'ON' if new_cascade else 'OFF'}",
                entity_type='System',
                entity_id=None,
            )
        settings.save()
        messages.success(request, 'Settings saved.')
        return redirect('admin_master_switches')

    return render(request, 'projects/admin/master_switches.html', {'settings': settings})


@login_required
@role_required(['Admin'])
def admin_user_management(request):
    """Screen 2: Activate/deactivate users and change roles."""
    from django.contrib.auth.models import User as AuthUser

    role_choices = UserProfile.ROLE_CHOICES

    if request.method == 'POST':
        action      = request.POST.get('action', '')
        target_id   = request.POST.get('user_id', '')
        actor       = request.user.profile

        try:
            target_user = AuthUser.objects.select_related('profile').get(pk=target_id)
        except AuthUser.DoesNotExist:
            messages.error(request, 'User not found.')
            return redirect('admin_user_management')

        # Never allow Admin to deactivate themselves
        if target_user == request.user and action == 'deactivate':
            messages.error(request, 'You cannot deactivate your own account.')
            return redirect('admin_user_management')

        if action == 'deactivate':
            target_user.is_active = False
            target_user.save()
            log_activity(
                project=None,
                actor=actor,
                action=f"User '{target_user.get_full_name() or target_user.username}' ({target_user.profile.role}) deactivated",
                entity_type='User',
                entity_id=target_user.id,
            )
            messages.success(request, f'{target_user.get_full_name() or target_user.username} deactivated.')

        elif action == 'reactivate':
            target_user.is_active = True
            target_user.save()
            log_activity(
                project=None,
                actor=actor,
                action=f"User '{target_user.get_full_name() or target_user.username}' ({target_user.profile.role}) reactivated",
                entity_type='User',
                entity_id=target_user.id,
            )
            messages.success(request, f'{target_user.get_full_name() or target_user.username} reactivated.')

        elif action == 'change_role':
            new_role = request.POST.get('new_role', '').strip()
            valid_roles = [r[0] for r in role_choices]
            if new_role not in valid_roles:
                messages.error(request, 'Invalid role selected.')
                return redirect('admin_user_management')
            old_role = target_user.profile.role
            if old_role != new_role:
                target_user.profile.role = new_role
                target_user.profile.save()
                log_activity(
                    project=None,
                    actor=actor,
                    action=f"User '{target_user.get_full_name() or target_user.username}' role changed from '{old_role}' to '{new_role}'",
                    entity_type='User',
                    entity_id=target_user.id,
                )
                messages.success(request, f'Role updated for {target_user.get_full_name() or target_user.username}.')
            else:
                messages.success(request, 'No change — role is already set to that value.')

        return redirect('admin_user_management')

    users = (
        AuthUser.objects
        .select_related('profile')
        .order_by('profile__role', 'first_name', 'last_name')
    )
    return render(request, 'projects/admin/user_management.html', {
        'users':        users,
        'role_choices': role_choices,
        'current_user': request.user,
    })


@login_required
@role_required(['Admin'])
def admin_notification_prefs(request):
    """Screen 3: Per-user WhatsApp and email notification toggles."""
    if request.method == 'POST':
        target_profile_id = request.POST.get('profile_id', '')
        actor = request.user.profile
        try:
            target_profile = UserProfile.objects.select_related('user').get(pk=target_profile_id)
        except UserProfile.DoesNotExist:
            messages.error(request, 'User not found.')
            return redirect('admin_notification_prefs')

        new_wa    = 'whatsapp_notifications' in request.POST
        new_email = 'email_notifications' in request.POST

        for field, label, new_val in [
            ('whatsapp_notifications', 'WhatsApp', new_wa),
            ('email_notifications',    'Email',    new_email),
        ]:
            old_val = getattr(target_profile, field)
            if old_val != new_val:
                setattr(target_profile, field, new_val)
                log_activity(
                    project=None,
                    actor=actor,
                    action=(
                        f"Notification pref updated for "
                        f"'{target_profile.user.get_full_name() or target_profile.user.username}': "
                        f"{label} set to {'ON' if new_val else 'OFF'}"
                    ),
                    entity_type='Notification',
                    entity_id=target_profile.id,
                )
        target_profile.save()
        messages.success(request, f'Preferences updated for {target_profile.user.get_full_name() or target_profile.user.username}.')
        return redirect('admin_notification_prefs')

    profiles = UserProfile.objects.select_related('user').order_by('role', 'user__first_name')
    return render(request, 'projects/admin/notification_prefs.html', {'profiles': profiles})


@login_required
@role_required(['Admin'])
def admin_departments(request):
    """Departments view — users grouped by role, with inline deactivate/reactivate/role-change."""
    from django.contrib.auth.models import User as AuthUser
    from itertools import groupby

    DEPT_NAMES = {
        'PM':            'Project Management',
        'Site Engineer': 'Site Execution',
        'SCM':           'Supply Chain',
        'Finance':       'Finance',
        'Design':        'Design',
        'CEO':           'Leadership',
        'BD':            'Sales & Business Development',
        'Admin':         'Administration',
    }

    if request.method == 'POST':
        action    = request.POST.get('action', '')
        target_id = request.POST.get('user_id', '')
        actor     = request.user.profile

        try:
            target_user = AuthUser.objects.select_related('profile').get(pk=target_id)
        except AuthUser.DoesNotExist:
            messages.error(request, 'User not found.')
            return redirect('admin_departments')

        if target_user == request.user and action == 'deactivate':
            messages.error(request, 'You cannot deactivate your own account.')
            return redirect('admin_departments')

        if action == 'deactivate':
            target_user.is_active = False
            target_user.save()
            log_activity(
                project=None,
                actor=actor,
                action=f"User '{target_user.get_full_name() or target_user.username}' ({target_user.profile.role}) deactivated",
                entity_type='User',
                entity_id=target_user.id,
            )
            messages.success(request, f'{target_user.get_full_name() or target_user.username} deactivated.')

        elif action == 'reactivate':
            target_user.is_active = True
            target_user.save()
            log_activity(
                project=None,
                actor=actor,
                action=f"User '{target_user.get_full_name() or target_user.username}' ({target_user.profile.role}) reactivated",
                entity_type='User',
                entity_id=target_user.id,
            )
            messages.success(request, f'{target_user.get_full_name() or target_user.username} reactivated.')

        elif action == 'change_role':
            new_role    = request.POST.get('new_role', '').strip()
            valid_roles = [r[0] for r in UserProfile.ROLE_CHOICES]
            if new_role not in valid_roles:
                messages.error(request, 'Invalid role selected.')
                return redirect('admin_departments')
            old_role = target_user.profile.role
            if old_role != new_role:
                target_user.profile.role = new_role
                target_user.profile.save()
                log_activity(
                    project=None,
                    actor=actor,
                    action=f"User '{target_user.get_full_name() or target_user.username}' role changed from '{old_role}' to '{new_role}'",
                    entity_type='User',
                    entity_id=target_user.id,
                )
                messages.success(request, f'Role updated for {target_user.get_full_name() or target_user.username}.')

        return redirect('admin_departments')

    all_profiles = (
        UserProfile.objects
        .select_related('user')
        .order_by('role', 'user__first_name', 'user__last_name')
    )

    grouped = []
    for role_key, members in groupby(all_profiles, key=lambda p: p.role):
        member_list = list(members)
        active_count = sum(1 for m in member_list if m.user.is_active)
        grouped.append({
            'role':         role_key,
            'dept_name':    DEPT_NAMES.get(role_key, role_key),
            'members':      member_list,
            'active_count': active_count,
        })

    return render(request, 'projects/admin/departments.html', {
        'grouped':      grouped,
        'role_choices': UserProfile.ROLE_CHOICES,
        'current_user': request.user,
    })


@login_required
@role_required(['Admin'])
def admin_user_edit(request, user_id):
    """Edit a user's full profile from the Admin Panel: name, username, email, phone, role, password."""
    from django.contrib.auth.models import User as AuthUser
    target_user = get_object_or_404(AuthUser, pk=user_id)
    try:
        profile = target_user.profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=target_user)

    if request.method == 'POST':
        form = AdminUserEditForm(request.POST, instance_user=target_user)
        if form.is_valid():
            cd = form.cleaned_data
            actor = request.user.profile

            target_user.first_name = cd['first_name']
            target_user.last_name  = cd['last_name']
            target_user.username   = cd['username']
            target_user.email      = cd['email']
            target_user.is_staff   = (cd['role'] == 'Admin')
            if cd['new_password']:
                target_user.set_password(cd['new_password'])
            target_user.save()

            profile.role         = cd['role']
            profile.phone_number = cd['phone_number']
            profile.save()

            log_activity(
                project=None,
                actor=actor,
                action=f"User '{target_user.get_full_name() or target_user.username}' profile updated by admin",
                entity_type='User',
                entity_id=target_user.id,
            )
            if cd['new_password']:
                log_activity(
                    project=None,
                    actor=actor,
                    action=f"Password reset for user '{target_user.username}' by admin",
                    entity_type='User',
                    entity_id=target_user.id,
                )

            messages.success(request, f'{target_user.get_full_name() or target_user.username} updated successfully.')
            return redirect('admin_departments')
    else:
        form = AdminUserEditForm(
            initial={
                'first_name':   target_user.first_name,
                'last_name':    target_user.last_name,
                'username':     target_user.username,
                'email':        target_user.email,
                'phone_number': profile.phone_number,
                'role':         profile.role,
            },
            instance_user=target_user,
        )

    return render(request, 'projects/admin/user_edit.html', {
        'form':        form,
        'target_user': target_user,
    })


@login_required
@role_required(['Admin'])
def admin_send_records(request):
    """Screen 5: Notification send log — all channels with filters and CSV export."""
    import csv
    from django.http import HttpResponse
    from django.core.paginator import Paginator

    channel_param   = request.GET.get('channel',   '').strip()
    status_param    = request.GET.get('status',    '').strip()
    date_from_param = request.GET.get('date_from', '').strip()
    date_to_param   = request.GET.get('date_to',   '').strip()
    export          = request.GET.get('export',    '').strip()

    default_date_from = (timezone.now().date() - timedelta(days=7)).isoformat()
    date_from = date_from_param or default_date_from

    qs = (
        NotificationLog.objects
        .select_related('recipient__user', 'related_project', 'actor__user')
        .order_by('-created_at')
    )

    if channel_param and channel_param != 'all':
        qs = qs.filter(channel=channel_param)
    if status_param and status_param != 'all':
        qs = qs.filter(status=status_param)
    if date_from:
        try:
            qs = qs.filter(created_at__date__gte=date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to_param:
        try:
            qs = qs.filter(created_at__date__lte=date.fromisoformat(date_to_param))
        except ValueError:
            pass

    # Stat cards — always last 7 days, unfiltered by channel/status
    seven_days_ago = timezone.now() - timedelta(days=7)
    stats_qs = NotificationLog.objects.filter(created_at__gte=seven_days_ago)
    stat_total     = stats_qs.count()
    stat_whatsapp  = stats_qs.filter(channel='whatsapp').count()
    stat_email     = stats_qs.filter(channel='email').count()
    stat_failed    = stats_qs.filter(status='failed').count()

    if export == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="send_records.csv"'
        writer = csv.writer(response)
        writer.writerow(['timestamp', 'recipient', 'role', 'channel', 'template', 'api_status', 'delivery_status'])
        for log in qs.iterator():
            writer.writerow([
                log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                log.recipient.user.get_full_name() or log.recipient.user.username,
                log.recipient.role,
                log.channel,
                log.template_name,
                log.status,
                log.delivery_status,
            ])
        return response

    paginator   = Paginator(qs, 50)
    page_number = request.GET.get('page')
    page_obj    = paginator.get_page(page_number)

    return render(request, 'projects/admin/send_records.html', {
        'page_obj':    page_obj,
        'stat_total':  stat_total,
        'stat_wa':     stat_whatsapp,
        'stat_email':  stat_email,
        'stat_failed': stat_failed,
        'filters': {
            'channel':   channel_param,
            'status':    status_param,
            'date_from': date_from,
            'date_to':   date_to_param,
        },
    })


# ---------------------------------------------------------------------------
# Admin — Audit Log
# ---------------------------------------------------------------------------

def _export_audit_csv(qs):
    import csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="audit_log.csv"'
    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'User', 'Role', 'Action', 'Entity Type', 'Entity ID', 'Project'])
    for entry in qs:
        writer.writerow([
            entry.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            entry.actor.user.get_full_name() if entry.actor else '—',
            entry.actor.role if entry.actor else '—',
            entry.action,
            entry.entity_type,
            entry.entity_id or '—',
            entry.project.project_id if entry.project else '—',
        ])
    return response


@login_required
@role_required(['Admin'])
def admin_audit_log(request):
    """Full audit log across all users and projects. Admin only."""
    from django.core.paginator import Paginator

    qs = ActivityLog.objects.select_related('actor__user', 'project').order_by('-timestamp')

    user_id   = request.GET.get('user', '').strip()
    entity    = request.GET.get('entity_type', '').strip()
    project   = request.GET.get('project', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to', '').strip()
    keyword   = request.GET.get('keyword', '').strip()

    if user_id:
        qs = qs.filter(actor__user__id=user_id)
    if entity:
        qs = qs.filter(entity_type=entity)
    if project:
        qs = qs.filter(project__project_id=project)
    if date_from:
        try:
            qs = qs.filter(timestamp__date__gte=date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            qs = qs.filter(timestamp__date__lte=date.fromisoformat(date_to))
        except ValueError:
            pass
    if keyword:
        qs = qs.filter(action__icontains=keyword)

    if request.GET.get('export') == 'csv':
        return _export_audit_csv(qs)

    seven_days_ago = timezone.now() - timedelta(days=7)
    stats = {
        'today': ActivityLog.objects.filter(timestamp__date=timezone.now().date()).count(),
        'this_week': ActivityLog.objects.filter(timestamp__gte=seven_days_ago).count(),
        'admin_actions': ActivityLog.objects.filter(
            timestamp__gte=seven_days_ago,
            entity_type__in=['System', 'User', 'Notification'],
        ).count(),
        'most_active': (
            ActivityLog.objects
            .filter(timestamp__gte=seven_days_ago)
            .values('actor__user__first_name', 'actor__user__last_name', 'actor__role')
            .annotate(cnt=Count('id'))
            .order_by('-cnt')
            .first()
        ),
    }

    paginator = Paginator(qs, 50)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    all_users    = UserProfile.objects.select_related('user').order_by('user__first_name')
    all_projects = Project.objects.filter(is_deleted=False).order_by('project_id')
    entity_types = ['Task', 'Issue', 'Project', 'Milestone', 'File', 'BOQ', 'Comment',
                    'User', 'System', 'Notification']

    return render(request, 'projects/admin/audit_log.html', {
        'page_obj':     page_obj,
        'stats':        stats,
        'all_users':    all_users,
        'all_projects': all_projects,
        'entity_types': entity_types,
        'filters': {
            'user':        user_id,
            'entity_type': entity,
            'project':     project,
            'date_from':   date_from,
            'date_to':     date_to,
            'keyword':     keyword,
        },
    })


@login_required
@role_required(['Admin'])
def admin_project_list(request):
    """All projects table inside the Admin Panel. Admin only."""
    projects = (
        Project.objects
        .filter(is_deleted=False)
        .select_related('assigned_pm__user', 'assigned_site_engineer__user')
        .prefetch_related(
            Prefetch('phases',
                     queryset=ProjectPhase.objects.prefetch_related('tasks').order_by('phase_order'))
        )
        .order_by('-created_at')
    )
    pm_users = (
        UserProfile.objects
        .filter(role='PM', is_active=True)
        .select_related('user')
        .order_by('user__first_name')
    )
    return render(request, 'projects/admin/projects_list.html', {
        'projects': projects,
        'pm_users': pm_users,
    })


@login_required
@role_required(['Admin'])
def admin_assign_pm(request, project_id):
    """Assign (or reassign) a PM to a Draft project. POST only. Admin only."""
    if request.method != 'POST':
        return redirect('admin_project_list')

    project = get_object_or_404(Project, project_id=project_id, is_deleted=False)

    pm_user_id = request.POST.get('pm_user_id', '').strip()
    if not pm_user_id:
        messages.error(request, 'Please select a PM.')
        return redirect('admin_project_list')

    try:
        pm_profile = UserProfile.objects.select_related('user').get(pk=pm_user_id, role='PM', is_active=True)
    except UserProfile.DoesNotExist:
        messages.error(request, 'Selected user is not a valid active PM.')
        return redirect('admin_project_list')

    old_pm = project.assigned_pm
    project.assigned_pm = pm_profile
    project.save(update_fields=['assigned_pm'])

    log_activity(
        project=project,
        actor=request.user,
        action=(
            f"PM assigned to {pm_profile.user.get_full_name() or pm_profile.user.username}"
            + (f" (previously {old_pm.user.get_full_name() or old_pm.user.username})" if old_pm else "")
        ),
        entity_type='Project',
        entity_id=project.pk,
    )

    # Notify the newly assigned PM
    try:
        pm_display_name = pm_profile.user.get_full_name() or pm_profile.user.username
        _link = f'/projects/{project.project_id}/overview/'
        _abs_link = request.build_absolute_uri(_link)
        _body = (
            f'Hi {pm_display_name},\n\n'
            f'You have been assigned as Project Manager for {project.customer_name}'
            + (f' ({project.city})' if project.city else '') + '.\n\n'
            f'Please review the project details and activate when ready.'
            f'\n\nView in Horizon Solar PMS:\nhttps://horizon-solar-pms-production.up.railway.app{_link}'
        )
        send_notification(
            recipient=pm_profile,
            message=_body,
            channels=['in_app', 'whatsapp', 'email'],
            link=_link,
            subject=f'New Project Assigned: {project.customer_name}',
            template='assign_project',
            template_params=[
                project.customer_name,
                pm_display_name,
                _abs_link,
            ],
            related_project=project,
        )
    except Exception as exc:
        logger.error('admin_assign_pm: notification failed for project %s — %s', project.project_id, exc)

    messages.success(
        request,
        f'PM assigned to {project.project_id}: {pm_profile.user.get_full_name() or pm_profile.user.username}.'
    )
    return redirect('admin_project_list')
