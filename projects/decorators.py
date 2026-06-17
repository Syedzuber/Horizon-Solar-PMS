import logging
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

logger = logging.getLogger(__name__)

# Maps every role to its landing dashboard URL.
# Used by login_view and role_required to redirect users after auth.
ROLE_DASHBOARD = {
    'Admin':         '/dashboard/admin/',
    'PM':            '/dashboard/pm/',
    'Site Engineer': '/dashboard/site-engineer/',
    'Design':        '/dashboard/design/',
    'Finance':       '/dashboard/finance/',
    'SCM':           '/dashboard/scm/',
    'CEO':           '/dashboard/ceo/',
    'BD':            '/dashboard/bd/',
}


def get_user_dashboard(user):
    """
    Return the correct dashboard URL for the given user based on their role.
    Falls back to /dashboard/admin/ if the user has no UserProfile — prevents
    a crash for superusers created via manage.py createsuperuser.
    """
    try:
        role = user.profile.role
        return ROLE_DASHBOARD.get(role, '/dashboard/admin/')
    except Exception:
        # TODO: role decorator assumes UserProfile exists — crashes for
        # admin-created users without a profile. Fix before scaling.
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'


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
