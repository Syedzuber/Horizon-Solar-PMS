import json
import logging
import re
import uuid as _uuid
from datetime import date, timedelta, datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby
from urllib.parse import urlparse as _urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import (
    UserProfile, Project, ProjectPhase, Task, DueDateChangeLog,
    Vendor, VendorCategory,
    BOQ, BOQItem, BOQRevision, Notification, get_standard_boq_items,
    PaymentMilestone, ProjectDocument, TaskAttachment,
    Issue, ActivityLog, Comment, log_activity,
    DeliveryChallan, DCLineItem, recalculate_dc_status, get_material_status,
)
from .forms import UserCreateForm, UserEditForm, ProjectCreateForm, ProjectEditForm, TaskAddForm, VendorForm
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

    projects_with_progress = []
    for project in Project.objects.filter(assigned_pm=pm_profile, status__in=['Active', 'In Progress']):
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
        })

    projects_with_progress.sort(
        key=lambda row: (row['overdue_count'] + row['blocked_count'], row['is_delayed']),
        reverse=True,
    )

    # Annotate material summary badges — single query across all projects, not N+1 per project
    # Fetch all delivery line item data in one query, then build a lookup dict
    if projects_with_progress:
        pm_project_ids = [row['project'].project_id for row in projects_with_progress]
        # Aggregate ordered/received totals and damage flag per project per category
        delivery_rows = DCLineItem.objects.filter(
            challan__project__project_id__in=pm_project_ids
        ).values(
            'challan__project__project_id', 'boq_category'
        ).annotate(
            total_ordered=Sum('ordered_quantity'),
            total_received=Sum('received_quantity'),
            damage_count=Count('pk', filter=Q(condition__in=['Damaged', 'Partial'])),
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
        for row in projects_with_progress:
            row['material_summary'] = _project_material_badge(
                row['project'].project_id, delivery_lookup
            )

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

    return render(request, 'dashboard/pm.html', {
        'summary': {
            'active_projects':  active_projects,
            'due_today':        due_today,
            'blocked_tasks':    blocked_tasks,
            'pending_approvals': pending_approvals,
            'external_pending': external_pending,
        },
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
    """
    Site Engineer dashboard: tasks due today, in-progress, and overdue
    across all projects the SE is assigned to.
    Access: Site Engineer only.
    """
    se_profile  = request.user.profile
    my_projects = Project.objects.filter(
        assigned_site_engineer=se_profile,
        status__in=['Active', 'In Progress'],
    )
    # Match tasks explicitly assigned to this SE, or unassigned SE-role tasks on their projects
    se_q = Q(assigned_to=se_profile) | Q(assigned_to__isnull=True, assigned_role=Task.SITE_ENGINEER)

    due_today = Task.objects.filter(phase__project__in=my_projects).filter(se_q).filter(
        due_date=date.today(), due_date__isnull=False,
        status__in=['Not Started', 'In Progress'],
    ).count()

    in_progress = Task.objects.filter(phase__project__in=my_projects).filter(se_q).filter(
        status='In Progress',
    ).count()

    overdue = Task.objects.filter(phase__project__in=my_projects).filter(se_q).filter(
        due_date__lt=date.today(), due_date__isnull=False,
        status__in=['Not Started', 'In Progress'],
    ).count()

    tasks_by_project = [
        {
            'project': project,
            'tasks': Task.objects.filter(phase__project=project).filter(se_q).order_by('due_date'),
        }
        for project in my_projects
    ]

    seven_days_ago = date.today() - timedelta(days=7)
    due_date_changes = DueDateChangeLog.objects.filter(
        task__phase__project__in=my_projects,
        task__assigned_role=Task.SITE_ENGINEER,
        changed_at__date__gte=seven_days_ago,
    ).select_related('task__phase__project', 'changed_by__user').order_by('-changed_at')[:20]

    return render(request, 'dashboard/site-engineer.html', {
        'summary': {
            'due_today':   due_today,
            'in_progress': in_progress,
            'overdue':     overdue,
        },
        'tasks_by_project':  tasks_by_project,
        'due_date_changes':  due_date_changes,
        'today':             date.today(),
        'all_profiles':      UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
@role_required(['Design'])
def dashboard_design(request):
    """
    Design dashboard: task counts and BOQ revision requests for projects
    where this user is assigned_design. Access: Design only.
    """
    design_profile  = request.user.profile
    project_filter  = {
        'assigned_role': Task.DESIGN,
        'phase__project__assigned_design': design_profile,
        'phase__project__status__in': ['Active', 'In Progress'],
    }

    try:
        due_today = Task.objects.filter(**project_filter).filter(
            due_date=date.today(), due_date__isnull=False,
            status__in=['Not Started', 'In Progress'],
        ).count()

        in_progress = Task.objects.filter(**project_filter).filter(
            status='In Progress',
        ).count()

        pending = Task.objects.filter(**project_filter).filter(
            status='Not Started',
        ).count()

        tasks_qs = Task.objects.filter(**project_filter).filter(
            due_date__isnull=False,
        ).select_related('phase__project').order_by(
            'phase__project__project_id', 'phase__phase_order', 'task_order',
        )

        boq_revision_requested = BOQ.objects.filter(
            status='Revision Requested',
            project__assigned_design=design_profile,
        ).count()

        assigned_projects = list(
            Project.objects.filter(
                assigned_design=design_profile,
                status__in=['Active', 'In Progress'],
            ).order_by('project_id')
        )
        for proj in assigned_projects:
            try:
                proj.boq_status = proj.boq.status
                proj.boq_url    = f'/projects/{proj.project_id}/boq/'
            except Exception:
                proj.boq_status = None
                proj.boq_url    = None

    except Exception:
        due_today = in_progress = pending = boq_revision_requested = 0
        tasks_qs          = Task.objects.none()
        assigned_projects = []

    seven_days_ago = date.today() - timedelta(days=7)
    due_date_changes = DueDateChangeLog.objects.filter(
        task__assigned_role=Task.DESIGN,
        task__phase__project__assigned_design=design_profile,
        task__phase__project__status__in=['Active', 'In Progress'],
        changed_at__date__gte=seven_days_ago,
    ).select_related('task__phase__project', 'changed_by__user').order_by('-changed_at')[:20]

    return render(request, 'dashboard/design.html', {
        'summary': {
            'due_today':              due_today,
            'in_progress':            in_progress,
            'pending':                pending,
            'boq_revision_requested': boq_revision_requested,
        },
        'tasks_qs':          tasks_qs,
        'assigned_projects': assigned_projects,
        'due_date_changes':  due_date_changes,
        'today':             date.today(),
        'all_profiles':      UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
@role_required(['Finance'])
def dashboard_finance(request):
    """
    Finance dashboard: aggregate payment totals and per-project M1/M2/M3 table.
    Access: Finance only.
    """
    today = timezone.now().date()

    total_pending = PaymentMilestone.objects.filter(
        status='Pending'
    ).aggregate(s=Sum('amount'))['s'] or 0

    total_invoiced = PaymentMilestone.objects.filter(
        status='Invoiced'
    ).aggregate(s=Sum('amount'))['s'] or 0

    total_received = PaymentMilestone.objects.filter(
        status='Received'
    ).aggregate(s=Sum('amount_received'))['s'] or 0

    overdue_count = PaymentMilestone.objects.filter(
        status='Pending',
        due_date__lt=today,
        due_date__isnull=False,
    ).count()

    projects = (
        Project.objects.filter(status__in=['Active', 'In Progress'])
        .prefetch_related('milestones')
        .select_related('assigned_pm__user')
        .order_by('project_id')
    )

    projects_with_milestones = []
    for project in projects:
        milestone_map = {'M1': None, 'M2': None, 'M3': None}
        for m in project.milestones.all():
            if m.milestone_name in milestone_map:
                if m.amount is not None and m.amount_received is not None:
                    m.variance = m.amount - m.amount_received
                else:
                    m.variance = None
                milestone_map[m.milestone_name] = m
        projects_with_milestones.append({
            'project': project,
            'M1': milestone_map['M1'],
            'M2': milestone_map['M2'],
            'M3': milestone_map['M3'],
        })

    return render(request, 'dashboard/finance.html', {
        'total_pending':            total_pending,
        'total_invoiced':           total_invoiced,
        'total_received':           total_received,
        'overdue_count':            overdue_count,
        'projects_with_milestones': projects_with_milestones,
        'today':                    today,
    })


@login_required
@role_required(['SCM'])
def dashboard_scm(request):
    """
    SCM dashboard: BOQs awaiting acknowledgment, procurement POs by project,
    delivery tasks, and overdue counts. Access: SCM only.
    """
    scm_profile   = request.user.profile
    # Match tasks explicitly assigned to this SCM user, or unassigned SCM-role tasks
    scm_q         = Q(assigned_to=scm_profile) | Q(assigned_to__isnull=True, assigned_role=Task.SCM)
    active_filter = {'phase__project__status__in': ['Active', 'In Progress']}

    boq_awaiting = BOQ.objects.filter(status='Submitted').count()

    deliveries_today = Task.objects.filter(
        assigned_role=Task.SCM,
        phase__phase_name='Delivery',
        due_date=date.today(), due_date__isnull=False,
        **active_filter,
    ).count()

    overdue = Task.objects.filter(
        assigned_role=Task.SCM,
        due_date__lt=date.today(), due_date__isnull=False,
        status__in=['Not Started', 'In Progress'],
        **active_filter,
    ).count()

    raw_procurement = Task.objects.filter(
        phase__phase_name='Procurement', **active_filter,
    ).filter(scm_q).select_related('phase__project').order_by(
        'phase__project__id', 'task_order',
    )
    pos_by_project = [
        (project, list(tasks))
        for project, tasks in groupby(raw_procurement, key=lambda t: t.phase.project)
    ]

    delivery_tasks = Task.objects.filter(
        phase__phase_name='Delivery', **active_filter,
    ).filter(scm_q).select_related('phase__project').order_by(
        'due_date', 'phase__project__project_id',
    )

    boqs = BOQ.objects.filter(
        project__status__in=['Active', 'In Progress']
    ).select_related('project').order_by('project__project_id')

    return render(request, 'dashboard/scm.html', {
        'summary': {
            'boq_awaiting':     boq_awaiting,
            'deliveries_today': deliveries_today,
            'overdue':          overdue,
        },
        'pos_by_project': pos_by_project,
        'delivery_tasks': delivery_tasks,
        'boqs':           boqs,
        'today':          date.today(),
        'all_profiles':   UserProfile.objects.select_related('user').filter(is_active=True).order_by('user__first_name'),
    })


@login_required
def dashboard_ceo(request):
    """CEO landing page — read-only summary view. Access: any authenticated user."""
    return render(request, 'dashboard/ceo.html')


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
    List all projects. PM sees only their own; Admin and CEO see all.
    Prefetches phases→tasks so get_current_phase() works without N+1.
    Access: PM (own only), Admin, CEO.
    """
    role = _get_user_role(request)
    base_qs = (
        Project.objects
        .select_related('assigned_pm__user', 'assigned_site_engineer__user')
        .prefetch_related(
            Prefetch('phases',
                     queryset=ProjectPhase.objects.prefetch_related('tasks').order_by('phase_order'))
        )
        .order_by('-created_at')
    )
    if role == 'PM':
        projects = base_qs.filter(assigned_pm__user=request.user)
    else:
        projects = base_qs

    return render(request, 'projects/project_list.html', {'projects': projects})


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
            return redirect('project_detail', project_id=project.project_id)
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
        return redirect('project_detail', project_id=project.project_id)

    if request.method == 'POST':
        form = ProjectEditForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            messages.success(request, f"Project {project.project_id} updated successfully.")
            return redirect('project_detail', project_id=project.project_id)
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
        return redirect('project_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    # Status check before any DB write — avoids partial state if already active
    if project.status != 'Draft':
        messages.warning(request, 'Project is already active.')
        return redirect('project_detail', project_id=project.project_id)

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
    log_activity(project, request.user.profile, f"Activated project: {project.project_id}", entity_type='', entity_id=None)

    if project.project_type == 'Residential':
        messages.success(request, 'Project activated. 50 tasks created. Set the first task due date to calculate all dates.')
    else:
        messages.success(request, 'Project activated. Add tasks manually using Add Task.')

    return redirect('project_detail', project_id=project.project_id)


@login_required
@role_required(['PM'])
def project_recalculate_dates(request, project_id):
    """
    Recalculate all task due dates from project.activated_at using the duration_days chain.
    Intended as a bulk reset; PM normally sets dates task-by-task via task_set_due_date.
    Access: assigned PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    if project.status == 'Draft':
        messages.warning(request, 'Project must be activated before calculating due dates.')
        return redirect('project_detail', project_id=project.project_id)

    if not project.activated_at:
        messages.warning(request, 'Project has no activation date — cannot calculate due dates.')
        return redirect('project_detail', project_id=project.project_id)

    calculate_due_dates(project)
    messages.success(request, 'Due dates recalculated from activation date.')
    return redirect('project_detail', project_id=project.project_id)


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
        return redirect('project_detail', project_id=project.project_id)

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
            return redirect('project_detail', project_id=project.project_id)
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
        Task.NOT_STARTED: {Task.IN_PROGRESS, Task.BLOCKED},
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

    # Server-side guard: In Progress requires a due date
    if new_status == Task.IN_PROGRESS and not task.due_date:
        messages.warning(request, 'Please set a due date before marking this task as In Progress.')
        return redirect('project_overview', project_id=project.project_id)

    update_kwargs = {'status': new_status}
    if new_status == Task.DONE:
        update_kwargs['completed_at'] = timezone.now()

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
        else:
            Task.objects.filter(pk=task.pk).update(assigned_to=None)
        return redirect('project_overview', project_id=project.project_id)

    return render(request, 'projects/task_assign_form.html', {
        'project':    project,
        'task':       task,
        'candidates': candidates,
    })


@login_required
@role_required(['PM'])
def task_set_due_date(request, project_id, task_id):
    """
    Set the due date on a task and cascade-recalculate all subsequent task due dates.
    Clearing the date (empty string) sets due_date=None without cascading.
    Access: assigned PM only. POST only.
    """
    if request.method != 'POST':
        return redirect('project_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    date_str = request.POST.get('due_date', '').strip()
    if date_str:
        try:
            new_date = date.fromisoformat(date_str)
            count = recalculate_from_task(project, task, new_date, user=request.user)
            messages.success(request, f'Due date updated. {count} task(s) recalculated.')
            # Log the due date update on successful recalculation
            log_activity(project, request.user.profile, f"Updated due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
        except ValueError:
            messages.error(request, 'Invalid date.')
    else:
        task.due_date = None
        task.save()
        messages.success(request, 'Due date cleared.')
        # Log the due date clear
        log_activity(project, request.user.profile, f"Updated due date for task: {task.task_name}", entity_type='Task', entity_id=task.pk)
    return redirect('project_detail', project_id=project.project_id)


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


@login_required
def vendor_add(request):
    """
    Add a new vendor. Warns (but does not block) if a vendor with the same name
    already exists — duplicate names are allowed to handle trading variants.
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
        'form':  form,
        'title': 'Add Vendor',
    })


@login_required
def vendor_edit(request, vendor_id):
    """Edit an existing vendor's details. Access: SCM and Admin only."""
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()

    vendor = get_object_or_404(Vendor, pk=vendor_id)

    if request.method == 'POST':
        form = VendorForm(request.POST, instance=vendor)
        if form.is_valid():
            form.save()
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
        'form':   form,
        'vendor': vendor,
        'title':  f'Edit Vendor — {vendor.name}',
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
    """Return a JSON-safe snapshot of all BOQ items (Decimal → float)."""
    import decimal as _decimal
    rows = list(boq.items.values(
        'serial_no', 'category', 'description', 'uom',
        'boq_quantity', 'ordered_quantity',
        'make_preference__name', 'ordered_vendor__name',
    ))
    for row in rows:
        for k, v in row.items():
            if isinstance(v, _decimal.Decimal):
                row[k] = float(v)
    return rows


def _notify_boq_acknowledged(boq, acknowledging_profile):
    """Create a Notification for the Design user who submitted the BOQ."""
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
    Notification.objects.create(
        recipient=boq.submitted_by,
        message=message,
        link=f'/projects/{boq.project.project_id}/boq/',
    )


def _build_vendors_by_category():
    """Return dict mapping category name → list of {id, name} for active vendors."""
    result = {}
    for vendor in Vendor.objects.filter(is_active=True).prefetch_related('categories').order_by('name'):
        for cat in vendor.categories.all():
            result.setdefault(cat.name, []).append({'id': vendor.pk, 'name': vendor.name})
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
            # Save ordered qty/vendor regardless of whether this is save or acknowledge
            for item in boq.items.all():
                qty_str    = request.POST.get(f'ord_qty_{item.pk}', '').strip()
                vendor_str = request.POST.get(f'ord_vendor_{item.pk}', '').strip()
                try:
                    item.ordered_quantity = Decimal(qty_str) if qty_str else None
                except InvalidOperation:
                    item.ordered_quantity = None
                item.ordered_vendor_id = int(vendor_str) if vendor_str.isdigit() else None
                item.save(update_fields=['ordered_quantity', 'ordered_vendor'])

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
                _notify_boq_acknowledged(boq, profile)

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

    items             = boq.items.select_related('make_preference', 'ordered_vendor').order_by('serial_no')
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
        'notifications': notifications,
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
    if milestone.status != 'Invoiced':
        messages.error(request, 'Only Invoiced milestones can be marked as Received.')
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
        return redirect('project_detail', project_id=project_id)

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
    return redirect('project_detail', project_id=project_id)


# ---------------------------------------------------------------------------
# BD dashboard
# ---------------------------------------------------------------------------

@login_required
@role_required(['BD'])
def dashboard_bd(request):
    """
    BD dashboard: high-level portfolio summary — contracted value, commissioned
    projects this month, pending payments, and per-project milestone status.
    Access: BD only.
    """
    today = timezone.now().date()

    active_qs = Project.objects.filter(status__in=['Active', 'In Progress'])

    commissioned_this_month = Project.objects.filter(
        status='Commissioned',
        commissioned_at__isnull=False,
        commissioned_at__month=today.month,
        commissioned_at__year=today.year,
    ).count()

    total_contracted = active_qs.aggregate(s=Sum('contract_value'))['s'] or 0

    pending_payments = PaymentMilestone.objects.filter(
        status__in=['Pending', 'Invoiced']
    ).count()

    projects_list = (
        active_qs
        .prefetch_related('milestones')
        .select_related('assigned_pm__user', 'assigned_site_engineer__user')
        .prefetch_related(
            Prefetch('phases',
                     queryset=ProjectPhase.objects.prefetch_related('tasks').order_by('phase_order'))
        )
        .order_by('project_id')
    )

    projects_with_data = []
    for project in projects_list:
        milestone_map = {'M1': None, 'M2': None, 'M3': None}
        for m in project.milestones.all():
            if m.milestone_name in milestone_map:
                milestone_map[m.milestone_name] = m

        current_phase = (
            project.phases
            .filter(tasks__status__in=['Not Started', 'In Progress', 'Blocked'])
            .order_by('phase_order').first()
            or project.phases.order_by('-phase_order').first()
        )

        projects_with_data.append({
            'project':       project,
            'M1':            milestone_map['M1'],
            'M2':            milestone_map['M2'],
            'M3':            milestone_map['M3'],
            'current_phase': current_phase,
        })

    return render(request, 'dashboard/bd.html', {
        'summary': {
            'active_projects':         active_qs.count(),
            'commissioned_this_month': commissioned_this_month,
            'total_contracted':        total_contracted,
            'pending_payments':        pending_payments,
        },
        'projects_with_data': projects_with_data,
        'today':              today,
    })


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

    # Handle POST actions (PM only) — formerly in project_detail view
    if request.method == 'POST' and is_assigned_pm:
        action = request.POST.get('action', '')

        if action == 'update_milestone':
            milestone_pk = request.POST.get('milestone_pk', '').strip()
            if milestone_pk:
                try:
                    milestone = project.milestones.get(pk=milestone_pk)
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

    # Milestones with variance
    milestones = list(project.milestones.all())
    for m in milestones:
        if m.amount is not None and m.amount_received is not None:
            m.variance = m.amount - m.amount_received
        else:
            m.variance = None

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

    # Delivery challans with per-DC line-item receipt summary
    delivery_challans = list(
        project.delivery_challans
        .select_related('vendor', 'created_by__user')
        .prefetch_related('line_items')
        .all()
    )
    for dc in delivery_challans:
        li_list   = list(dc.line_items.all())
        total     = len(li_list)
        confirmed = sum(1 for li in li_list if li.received_quantity is not None)
        dc.line_items_summary = (
            f"{confirmed} of {total} item{'s' if total != 1 else ''} received"
            if total else "No items logged"
        )

    # Per-category material status (summary badge + detail quantities)
    material_status = get_material_status(project)

    cat_rows = (
        DCLineItem.objects.filter(challan__project=project)
        .values('boq_category')
        .annotate(
            total_ordered=Sum('ordered_quantity'),
            total_received=Sum('received_quantity'),
            damage_count=Count('pk', filter=Q(condition__in=['Damaged', 'Partial'])),
        )
    )
    material_status_by_category = []
    for row in cat_rows:
        received   = row['total_received'] or 0
        ordered    = row['total_ordered'] or 0
        has_damage = row['damage_count'] > 0
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

    return render(request, 'projects/project_overview.html', {
        'project':                     project,
        'milestones':                  milestones,
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
    # Fall back to the default PM (chetan@horizonrenewablepower.com) if the Zoho
    # Assign_PM field is empty or the email doesn't match any active user
    if assigned_pm is None:
        assigned_pm = UserProfile.objects.filter(
            user__email__iexact='chetan@horizonrenewablepower.com'
        ).first()

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

    # Notify Admin only if PM fallback also failed (chetan@horizonrenewablepower.com not found)
    if project.assigned_pm is None:
        try:
            admin_profile = UserProfile.objects.filter(role='Admin').first()
            if admin_profile:
                Notification.objects.create(
                    recipient=admin_profile,
                    message=(
                        f'New Draft project {project.project_id} created from Zoho CRM '
                        f'(deal {record_id}) — PM could not be assigned. Please assign a PM.'
                    ),
                    link=f'/projects/{project.project_id}/',
                )
        except Exception as exc:
            logger.error('Webhook: notification creation failed for project %s — %s', project.project_id, exc)

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
        return redirect('project_detail', project_id=project_id)

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
    return redirect('project_detail', project_id=project_id)


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
        return redirect('project_detail', project_id=project_id)

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
    messages.success(request, f'Issue "{title}" raised.')
    return redirect('task_detail', project_id=project_id, task_id=task_id)


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
        if project.assigned_pm and project.assigned_pm != profile:
            resolver_name = profile.user.get_full_name() or profile.user.username
            Notification.objects.create(
                recipient=project.assigned_pm,
                message=(
                    f'{resolver_name} resolved issue "{issue.title}" on {project.project_id}. '
                    f'Please review and close.'
                ),
                link=f'/issues/{issue.pk}/',
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

    return render(request, 'projects/delivery_challan_detail.html', {
        'project':           project,
        'challan':           challan,
        'line_items':        line_items,
        'role':              profile.role,
        'user_profile':      profile,
        'condition_choices': DCLineItem.CONDITION_CHOICES,
    })


@login_required
@role_required(['Site Engineer'])
def confirm_grn(request, project_id, dc_id):
    """
    SE confirms receipt of materials at site (GRN confirmation).
    Damaged condition forces received_quantity=0 — client-submitted value is ignored
    to prevent recording usable quantity for items that arrived damaged.
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
        qty_str   = request.POST.get(f'received_qty_{item.pk}', '').strip()
        condition = request.POST.get(f'condition_{item.pk}', '').strip()
        grn_notes = request.POST.get(f'grn_notes_{item.pk}', '').strip()

        if not qty_str or not condition:
            continue
        if condition not in dict(DCLineItem.CONDITION_CHOICES):
            continue

        received_qty = _safe_decimal(qty_str)
        if received_qty is None:
            continue

        # Damaged items force received_quantity to 0 — nothing usable received
        if condition == DCLineItem.DAMAGED:
            received_qty = Decimal('0')

        item.received_quantity = received_qty
        item.condition         = condition
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
    No status restriction: SCM can override even on Received DCs (to correct mistakes).
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
        qty_str   = request.POST.get(f'received_qty_{item.pk}', '').strip()
        condition = request.POST.get(f'condition_{item.pk}', '').strip()
        grn_notes = request.POST.get(f'grn_notes_{item.pk}', '').strip()

        if not qty_str or not condition:
            continue
        if condition not in dict(DCLineItem.CONDITION_CHOICES):
            continue

        received_qty = _safe_decimal(qty_str)
        if received_qty is None:
            continue

        # Damaged items force received_quantity to 0
        if condition == DCLineItem.DAMAGED:
            received_qty = Decimal('0')

        item.received_quantity = received_qty
        item.condition         = condition
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
