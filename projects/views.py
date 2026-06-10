import logging
from datetime import date

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User
from django.http import Http404, HttpResponseForbidden
from django.utils import timezone
from django.db import transaction
from django.db.models import Max, Prefetch, Q

from .models import UserProfile, Project, ProjectPhase, Task
from .forms import UserCreateForm, UserEditForm, ProjectCreateForm, ProjectEditForm, TaskAddForm
from .decorators import login_required, role_required, get_user_dashboard
from .utils import attach_residential_template, calculate_due_dates

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

    return render(request, 'dashboard/site-engineer.html', {
        'summary': {
            'due_today':   due_today,
            'in_progress': in_progress,
            'overdue':     overdue,
        },
        'tasks_by_project': tasks_by_project,
        'today': date.today(),
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

    return render(request, 'dashboard/design.html', {
        'summary': {
            'due_today':   due_today,
            'in_progress': in_progress,
            'pending':     pending,
        },
        'tasks_qs': tasks_qs,
        'today':    date.today(),
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

    procurement_tasks = Task.objects.filter(
        phase__phase_name='Procurement', **active_filter,
    ).filter(scm_q).select_related('phase__project').order_by(
        'phase__project__project_id', 'task_order',
    )

    delivery_tasks = Task.objects.filter(
        phase__phase_name='Delivery', **active_filter,
    ).filter(scm_q).select_related('phase__project').order_by(
        'due_date', 'phase__project__project_id',
    )

    return render(request, 'dashboard/scm.html', {
        'summary': {
            'pos_pending':      pos_pending,
            'deliveries_today': deliveries_today,
            'overdue':          overdue,
        },
        'procurement_tasks': procurement_tasks,
        'delivery_tasks':    delivery_tasks,
        'today':             date.today(),
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

    return render(request, 'projects/project_detail.html', {
        'project':       project,
        'phases':        phases,
        'is_assigned_pm': is_assigned_pm,
        'user_role':     user_role,
        'task_status_choices': Task.STATUS_CHOICES,
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
            calculate_due_dates(project)

    if project.project_type == 'Residential':
        messages.success(request, 'Project activated. 50 tasks created across 9 phases.')
    else:
        messages.success(request, 'Project activated. Add tasks manually using Add Task.')

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
