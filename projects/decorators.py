import logging
from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponseForbidden

logger = logging.getLogger(__name__)

# Where a user with no UserProfile is sent by the navigation helpers below.
#
# 0.2 lockdown: these helpers used to answer '/dashboard/admin/' for a profile-less user,
# which is the navigational half of the same fail-open role_required() had. A user who
# holds no role has no dashboard, and saying otherwise put an "Admin" destination in front
# of somebody the authorisation layer refuses everywhere.
#
# login_view() carries a same-target guard so this cannot become a redirect loop for an
# already-authenticated caller.
NO_PROFILE_URL = '/login/'

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

    0.2 lockdown: a user with no UserProfile now resolves to NO_PROFILE_URL, not to
    /dashboard/admin/. The old fallback was the navigational half of role_required()'s
    fail-open — it answered "the admin dashboard is your home" for somebody every
    permissions.py helper refuses. Nothing downstream would have honoured that claim once
    the decorator started denying, so the URL was simply untrue.

    context: optional 'residential' | 'tenders'. When supplied for a LANDING_ROLES
    user, the context is appended as a query param so the dashboard filters itself.
    When omitted (the default) the return value is unchanged for every user who HAS a
    profile — this only alters the profile-less answer.
    """
    try:
        role = user.profile.role
        url = ROLE_DASHBOARD.get(role, '/dashboard/admin/')
    except Exception:
        logger.warning("No UserProfile for user %s — no dashboard to resolve", user.username)
        return NO_PROFILE_URL

    if context and role in LANDING_ROLES:
        return f'{url}?context={context}'
    return url


def get_post_login_url(user):
    """
    Where to send a user immediately after authenticating.

    CEO / Finance / SCM land on the context chooser; every other role keeps the
    exact previous behaviour (straight to their role dashboard). Used only by login_view.

    0.2 lockdown: profile-less resolves to NO_PROFILE_URL for the same reason
    get_user_dashboard() does. role_required() no longer redirects on denial at all — it
    returns 403 — so this helper is now only ever a post-login destination.
    """
    try:
        role = user.profile.role
    except Exception:
        logger.warning("No UserProfile for user %s — no post-login destination", user.username)
        return NO_PROFILE_URL

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
    Redirects unauthenticated users to /login/; DENIES everyone else with 403.

    TWO 0.2 LOCKDOWN CHANGES, AND BOTH ARE DELIBERATE.

    1. NO PROFILE NOW DENIES, INSTEAD OF BEING TREATED AS 'Admin'. The old fallback
       admitted a profile-less user to every one of the 33 @role_required(['Admin'])
       screens in views.py — the whole admin panel, master switches included — while every
       permissions.py helper correctly refused the same user (they all guard with
       `getattr(user, 'profile', None)` and return False). A `createsuperuser` account has
       no UserProfile, so "avoid a hard crash" had quietly become "hand out the admin
       panel". Holding no role is not a reason to be given the most powerful one.

       ⚠ DEPLOYMENT NOTE: if an installation relies on a profile-less superuser for
       administration, this locks that account out. The fix is to give it a UserProfile
       with role='Admin', not to restore the fallback. Verified before shipping: the
       development database has 66 users, one superuser (`admin`), and ZERO profile-less
       accounts — the superuser already carries role='Admin' and is unaffected.

    2. DENIAL IS NOW 403, NOT A REDIRECT WITH A FLASH MESSAGE. Views gated by
       permissions.py already returned HttpResponseForbidden, so the product had two
       denial semantics and a probe could tell "wrong role" (302 to a dashboard) from
       "not yours" (403) — which turns the difference into an oracle for whether a
       given object exists and who owns it. One authorisation failure, one response.

       This is the reason get_user_dashboard() is no longer called here.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('/login/')
            try:
                role = request.user.profile.role
            except Exception:
                # No UserProfile — no role, and therefore no access. Deny, never assume.
                logger.warning(
                    "No UserProfile for user %s in role_required check — denying",
                    request.user.username,
                )
                return HttpResponseForbidden()
            if role not in allowed_roles:
                return HttpResponseForbidden()
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


system_admin_required = role_required(['System Admin'])
