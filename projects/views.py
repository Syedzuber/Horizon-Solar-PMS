import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User
from django.http import Http404, HttpResponseForbidden
from django.utils import timezone
from django.db import transaction
from django.db.models import Max

from .models import UserProfile, Project, ProjectPhase, Task
from .forms import UserCreateForm, UserEditForm, ProjectCreateForm, ProjectEditForm, TaskAddForm
from .decorators import login_required, role_required, get_user_dashboard
from .utils import attach_residential_template

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
def dashboard_pm(request):
    return render(request, 'dashboard/pm.html')


@login_required
def dashboard_site_engineer(request):
    return render(request, 'dashboard/site-engineer.html')


@login_required
def dashboard_design(request):
    return render(request, 'dashboard/design.html')


@login_required
def dashboard_finance(request):
    return render(request, 'dashboard/finance.html')


@login_required
def dashboard_scm(request):
    return render(request, 'dashboard/scm.html')


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
    if role == 'PM':
        projects = Project.objects.filter(
            assigned_pm__user=request.user
        ).select_related('assigned_site_engineer__user').order_by('-created_at')
    else:
        projects = Project.objects.all().select_related(
            'assigned_site_engineer__user'
        ).order_by('-created_at')

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
        form = ProjectCreateForm()

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
        if project.project_type == 'Residential':
            attach_residential_template(project)

        project.status = 'Active'
        project.activated_at = timezone.now()
        project.save()

    if project.project_type == 'Residential':
        messages.success(request, 'Project activated. 51 tasks created across 9 phases.')
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

    return redirect('project_detail', project_id=project.project_id)
