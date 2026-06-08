import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.models import User

from .models import UserProfile
from .forms import UserCreateForm, UserEditForm
from .decorators import login_required, role_required, get_user_dashboard

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
            # Signal already created a blank profile; update it with real data
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
                'first_name':    target_user.first_name,
                'last_name':     target_user.last_name,
                'email':         target_user.email,
                'role':          profile.role,
                'phone_number':  profile.phone_number,
                'is_active':     profile.is_active,
            },
            instance_user=target_user,
        )

    return render(request, 'users/user_form.html', {
        'form': form,
        'action': 'Edit',
        'target_user': target_user,
    })
