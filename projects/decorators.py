import logging
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

logger = logging.getLogger(__name__)

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
    try:
        role = user.profile.role
        return ROLE_DASHBOARD.get(role, '/dashboard/admin/')
    except Exception:
        logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
        return '/dashboard/admin/'


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/login/')
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/login/')
            try:
                role = request.user.profile.role
            except Exception:
                logger.warning("No UserProfile for user %s in role_required check", request.user.username)
                role = 'Admin'
            if role not in allowed_roles:
                messages.error(request, "You don't have access to this page")
                return redirect(get_user_dashboard(request.user))
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
