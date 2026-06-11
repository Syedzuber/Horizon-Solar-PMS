import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.db import transaction
from django.db.models import Max, Prefetch, Q

from .models import (
    UserProfile, Project, ProjectPhase, Task, DueDateChangeLog,
    Vendor, VendorCategory,
    BOQ, BOQItem, BOQRevision, Notification, get_standard_boq_items,
)
from .forms import UserCreateForm, UserEditForm, ProjectCreateForm, ProjectEditForm, TaskAddForm, VendorForm
from .decorators import login_required, role_required, get_user_dashboard
from .utils import attach_residential_template, calculate_due_dates, recalculate_from_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_view(request):
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
    logout(request)
    return redirect('/login/')


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

@login_required
def dashboard_admin(request):
    return render(request, 'dashboard/admin.html')


@login_required
@role_required(['PM'])
def dashboard_pm(request):
    pm_profile = request.user.profile

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
        current_phase  = (
            project.phases
            .filter(tasks__status__in=['Not Started', 'In Progress', 'Blocked'])
            .order_by('phase_order').first()
            or project.phases.order_by('-phase_order').first()
        )
        projects_with_progress.append({
            'project':          project,
            'total_tasks':      total_tasks,
            'done_tasks':       done_tasks,
            'internal_total':   internal_total,
            'internal_done':    internal_done,
            'internal_percent': internal_percent,
            'external_pending': ext_pending,
            'overdue_count':    overdue_count,
            'current_phase':    current_phase,
        })

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
    })


@login_required
@role_required(['Site Engineer'])
def dashboard_site_engineer(request):
    se_profile  = request.user.profile
    my_projects = Project.objects.filter(
        assigned_site_engineer=se_profile,
        status__in=['Active', 'In Progress'],
    )
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
    })


@login_required
@role_required(['Design'])
def dashboard_design(request):
    design_profile = request.user.profile
    design_q       = Q(assigned_to=design_profile) | Q(assigned_to__isnull=True, assigned_role=Task.DESIGN)
    active_filter  = {'phase__project__status__in': ['Active', 'In Progress']}

    try:
        due_today = Task.objects.filter(**active_filter).filter(design_q).filter(
            due_date=date.today(), due_date__isnull=False,
            status__in=['Not Started', 'In Progress'],
        ).count()

        in_progress = Task.objects.filter(**active_filter).filter(design_q).filter(
            status='In Progress',
        ).count()

        pending = Task.objects.filter(**active_filter).filter(design_q).filter(
            status='Not Started',
        ).count()

        tasks_qs = Task.objects.filter(**active_filter).filter(design_q).filter(
            due_date__isnull=False,
        ).select_related('phase__project').order_by(
            'phase__project__project_id', 'phase__phase_order', 'task_order',
        )
    except Exception:
        due_today = in_progress = pending = 0
        tasks_qs  = Task.objects.none()

    seven_days_ago = date.today() - timedelta(days=7)
    due_date_changes = DueDateChangeLog.objects.filter(
        task__assigned_role=Task.DESIGN,
        task__phase__project__status__in=['Active', 'In Progress'],
        changed_at__date__gte=seven_days_ago,
    ).select_related('task__phase__project', 'changed_by__user').order_by('-changed_at')[:20]

    return render(request, 'dashboard/design.html', {
        'summary': {
            'due_today':   due_today,
            'in_progress': in_progress,
            'pending':     pending,
        },
        'tasks_qs':         tasks_qs,
        'due_date_changes': due_date_changes,
        'today':            date.today(),
    })


@login_required
def dashboard_finance(request):
    return render(request, 'dashboard/finance.html')


@login_required
@role_required(['SCM'])
def dashboard_scm(request):
    scm_profile   = request.user.profile
    scm_q         = Q(assigned_to=scm_profile) | Q(assigned_to__isnull=True, assigned_role=Task.SCM)
    active_filter = {'phase__project__status__in': ['Active', 'In Progress']}

    pos_pending = Task.objects.filter(
        assigned_role=Task.SCM,
        phase__phase_name='Procurement',
        status__in=['Not Started', 'In Progress'],
        **active_filter,
    ).count()

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

    # BOQ Status card — all active projects with their BOQ state
    boq_projects = []
    for project in Project.objects.filter(
        status__in=['Active', 'In Progress']
    ).order_by('project_id'):
        try:
            boq         = project.boq
            boq_status  = boq.status
            last_updated = boq.submitted_at
        except BOQ.DoesNotExist:
            boq          = None
            boq_status   = None
            last_updated = None
        boq_projects.append({
            'project':      project,
            'boq':          boq,
            'boq_status':   boq_status,
            'last_updated': last_updated,
        })

    return render(request, 'dashboard/scm.html', {
        'summary': {
            'pos_pending':      pos_pending,
            'deliveries_today': deliveries_today,
            'overdue':          overdue,
        },
        'pos_by_project':  pos_by_project,
        'delivery_tasks':  delivery_tasks,
        'boq_projects':    boq_projects,
        'today':           date.today(),
    })


@login_required
def dashboard_ceo(request):
    return render(request, 'dashboard/ceo.html')


# ---------------------------------------------------------------------------
# User management (Admin only)
# ---------------------------------------------------------------------------

@login_required
@role_required(['Admin'])
def user_list(request):
    profiles = UserProfile.objects.select_related('user', 'created_by').order_by('user__username')
    return render(request, 'users/user_list.html', {'profiles': profiles})


@login_required
@role_required(['Admin'])
def user_create(request):
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
    target_user = get_object_or_404(User, pk=user_id)
    try:
        profile = target_user.profile
    except UserProfile.DoesNotExist:
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
    try:
        return request.user.profile.role
    except Exception:
        return None


def _pm_owns_project(request, project):
    """Return True if the request user is the assigned PM on this project."""
    return project.assigned_pm.user == request.user


@login_required
@role_required(['PM', 'Admin', 'CEO'])
def project_list(request):
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
@role_required(['PM', 'Admin', 'CEO'])
def project_detail(request, project_id):
    project = get_object_or_404(
        Project.objects.select_related(
            'assigned_pm__user', 'assigned_site_engineer__user', 'created_by'
        ),
        project_id=project_id,
    )

    role = _get_user_role(request)
    if role == 'PM' and not _pm_owns_project(request, project):
        raise Http404

    phases = []
    if project.status != 'Draft':
        phases = (
            ProjectPhase.objects.filter(project=project)
            .prefetch_related('tasks')
            .order_by('phase_order')
        )

    is_assigned_pm = (project.assigned_pm.user == request.user)
    user_role = role

    candidates_by_role = {}
    if is_assigned_pm:
        for role_key, _ in Task.ROLE_CHOICES:
            qs = UserProfile.objects.filter(role=role_key, is_active=True).select_related('user')
            candidates_by_role[role_key] = [
                {'pk': p.pk, 'name': p.user.get_full_name() or p.user.username}
                for p in qs
            ]

    return render(request, 'projects/project_detail.html', {
        'project':             project,
        'phases':              phases,
        'is_assigned_pm':      is_assigned_pm,
        'user_role':           user_role,
        'task_status_choices': Task.STATUS_CHOICES,
        'candidates_by_role':  candidates_by_role,
    })


@login_required
@role_required(['PM'])
def project_edit(request, project_id):
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
    if request.method != 'POST':
        return redirect('project_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    # Failure 6: status check must be first, before any DB write
    if project.status != 'Draft':
        messages.warning(request, 'Project is already active.')
        return redirect('project_detail', project_id=project.project_id)

    with transaction.atomic():
        project.status = 'Active'
        project.activated_at = timezone.now()
        project.save()

        if project.project_type == 'Residential':
            attach_residential_template(project)

    if project.project_type == 'Residential':
        messages.success(request, 'Project activated. 50 tasks created. Set the first task due date to calculate all dates.')
    else:
        messages.success(request, 'Project activated. Add tasks manually using Add Task.')

    return redirect('project_detail', project_id=project.project_id)


@login_required
@role_required(['PM'])
def project_recalculate_dates(request, project_id):
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
    if request.method != 'POST':
        return redirect('project_detail', project_id=project_id)

    project = get_object_or_404(Project, project_id=project_id)
    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    # Failure 7: check permission before any DB write
    try:
        user_role = request.user.profile.role
    except Exception:
        user_role = None

    is_pm = _pm_owns_project(request, project)

    if user_role != task.assigned_role and not is_pm:
        return HttpResponseForbidden()

    new_status = request.POST.get('status', '').strip()
    valid_statuses = {s[0] for s in Task.STATUS_CHOICES}
    if new_status not in valid_statuses:
        messages.error(request, 'Invalid status value.')
        return redirect('project_detail', project_id=project.project_id)

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
        return redirect('project_detail', project_id=project.project_id)

    update_kwargs = {'status': new_status}
    if new_status == Task.DONE:
        update_kwargs['completed_at'] = timezone.now()

    Task.objects.filter(pk=task.pk).update(**update_kwargs)

    next_url = request.POST.get('next', None)
    if next_url:
        from urllib.parse import urlparse
        if urlparse(next_url).netloc == '':
            return redirect(next_url)
    return redirect('project_detail', project_id=project.project_id)


@login_required
@role_required(['PM'])
def task_assign(request, project_id, task_id):
    project = get_object_or_404(Project, project_id=project_id)

    if not _pm_owns_project(request, project):
        raise Http404

    task = get_object_or_404(Task, pk=task_id, phase__project=project)

    # Failure 9: dropdown must only show users matching task's assigned_role
    candidates = UserProfile.objects.filter(role=task.assigned_role, is_active=True)

    if request.method == 'POST':
        assigned_to_id = request.POST.get('assigned_to', '').strip()
        if assigned_to_id:
            assignee = get_object_or_404(UserProfile, pk=assigned_to_id, role=task.assigned_role, is_active=True)
            Task.objects.filter(pk=task.pk).update(assigned_to=assignee)
        else:
            Task.objects.filter(pk=task.pk).update(assigned_to=None)
        return redirect('project_detail', project_id=project.project_id)

    return render(request, 'projects/task_assign_form.html', {
        'project':    project,
        'task':       task,
        'candidates': candidates,
    })


@login_required
@role_required(['PM'])
def task_set_due_date(request, project_id, task_id):
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
        except ValueError:
            messages.error(request, 'Invalid date.')
    else:
        task.due_date = None
        task.save()
        messages.success(request, 'Due date cleared.')


# ---------------------------------------------------------------------------
# Vendor Master
# ---------------------------------------------------------------------------

@login_required
def vendor_list(request):
    profile = request.user.profile
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
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile
    role    = profile.role

    if role not in ('Design', 'SCM', 'PM', 'Admin'):
        return HttpResponseForbidden()

    # Get or auto-create BOQ for Design
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

    messages.success(request, 'BOQ submitted to SCM for review.')
    return redirect('boq_detail', project_id=project_id)


@login_required
def boq_acknowledge(request, project_id):
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

    messages.success(request, 'BOQ acknowledged.')
    return redirect('boq_detail', project_id=project_id)


@login_required
def boq_request_revision(request, project_id):
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
    project = get_object_or_404(Project, project_id=project_id)
    profile = request.user.profile

    if profile.role not in ('PM', 'Design', 'SCM', 'Admin'):
        return HttpResponseForbidden()

    boq = get_object_or_404(BOQ, project=project)
    # Chronological order so the timeline reads top-to-bottom oldest-first
    raw_revisions = boq.revisions.select_related('revised_by__user').order_by('revised_at')

    def _annotate(rev):
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
