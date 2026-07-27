import logging
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

logger = logging.getLogger(__name__)

# Maps every role to its landing dashboard URL.
# Used by login_view and role_required to redirect users after auth.
ROLE_DASHBOARD = {
    'Admin':         '/dashboard/admin/',
    'System Admin':  '/sub-admin/projects/',
    'PM':            '/dashboard/pm/',
    # Coordinators reuse the PM dashboard (scoped to their coordinated projects).
    # Without this entry the role fell back silently to the Admin dashboard — a
    # role-inappropriate-data-exposure risk, not a cosmetic bug.
    'Project Coordinator': '/dashboard/pm/',
    'Site Engineer': '/dashboard/site-engineer/',
    'Design':        '/dashboard/design/',
    'Finance':       '/dashboard/finance/',
    'SCM':           '/dashboard/scm/',
    'CEO':           '/dashboard/ceo/',
    'BD':            '/dashboard/bd/',
}


# Roles that see the two-context landing screen (EPC Residential / Tenders)
# after login instead of dropping straight onto their dashboard. These are the
# only three roles whose dashboards commingle Residential projects with OPEX /
# CAPEX tender sites, so they are the only ones that need to pick a context.
# This is a display concern only — it grants and removes no access.
LANDING_ROLES = ('CEO', 'Finance', 'SCM')

LANDING_URL = '/landing/'


def get_user_dashboard(user, context=None):
    """
    Return the correct dashboard URL for the given user based on their role.
    Falls back to /dashboard/admin/ if the user has no UserProfile — prevents
    a crash for superusers created via manage.py createsuperuser.

    context: optional 'residential' | 'tenders'. When supplied for a LANDING_ROLES
    user, the context is appended as a query param so the dashboard filters itself.
    When omitted (the default) the return value is byte-identical to before —
    every existing caller keeps its current behaviour.
    """
    try:
        role = user.profile.role
        url = ROLE_DASHBOARD.get(role, '/dashboard/admin/')
    except Exception:
        # TODO: role decorator assumes UserProfile exists — crashes for
        # admin-created users without a profile. Fix before scaling.
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'

    if context and role in LANDING_ROLES:
        return f'{url}?context={context}'
    return url


def get_post_login_url(user):
    """
    Where to send a user immediately after authenticating.

    CEO / Finance / SCM land on the context chooser; every other role keeps the
    exact previous behaviour (straight to their role dashboard). Used only by
    login_view — role_required's denial redirect deliberately still uses
    get_user_dashboard() so a wrong-role bounce is unchanged.
    """
    try:
        role = user.profile.role
    except Exception:
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'

    if role in LANDING_ROLES:
        return LANDING_URL
    return get_user_dashboard(user)


def login_required(view_func):
    """Redirect unauthenticated users to /login/. Used in place of Django's built-in."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/login/')
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(allowed_roles):
    """
    Restrict a view to users whose UserProfile.role is in allowed_roles.
    Redirects unauthenticated users to /login/.
    Redirects authenticated users without the right role back to their own dashboard.
    Falls back to 'Admin' role if the user has no UserProfile (avoids a hard crash).
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/login/')
            try:
                role = request.user.profile.role
            except Exception:
                # No UserProfile — treat as Admin so Django superusers can still navigate
                logger.warning("No UserProfile for user %s in role_required check", request.user.username)
                role = 'Admin'
            if role not in allowed_roles:
                messages.error(request, "You don't have access to this page")
                return redirect(get_user_dashboard(request.user))
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


system_admin_required = role_required(['System Admin'])
