import logging
from functools import wraps
from django.conf import settings
from django.shortcuts import redirect, render
from django.contrib import messages
from django.http import HttpResponseForbidden

# permissions.py imports nothing from this module (its only top-level import is Q), so
# this is not circular. user_can_view_site_dashboard is THE answer to "does this user
# land on the Site Dashboard" (DASH-B4 R1), the same helper that page itself calls;
# user_can_view_finance_work is the Finance work page's own check (FIN-M-B8b), asked
# before Finance is sent there so the landing can never be a page that refuses them.
from .permissions import user_can_view_finance_work, user_can_view_site_dashboard

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

#: The common Site Dashboard (DASH-B2). Since DASH-B4 it is the landing page and the
#: Home link of everyone user_can_view_site_dashboard() admits, except Finance (FIN-M-B8b).
SITE_DASHBOARD_URL = '/dashboard/sites/'

#: The Finance work page (FIN-M-B8a, views_finance_work.finance_work). Since FIN-M-B8b it
#: is Finance's landing page and Home link; the Site Dashboard stays one click away from
#: it ("Site list"). Written out like SITE_DASHBOARD_URL rather than reversed, because
#: urls.py imports views, which import this module; tests_finance_landing pins it equal
#: to reverse('finance_work').
FINANCE_WORK_URL = '/finance/work/'

# Maps every role to its landing dashboard URL.
# Used by login_view and role_required to redirect users after auth.
#
# DASH-B4 (R1): the six Site Dashboard roles map to SITE_DASHBOARD_URL. get_user_dashboard()
# asks user_can_view_site_dashboard() first, so for them this table is never read; it is
# kept true so a reader of the table is not misled. The Design Head is a flag on a 'Design'
# profile, not a role, so only the helper can send them there.
# FIN-M-B8b: Finance maps to FINANCE_WORK_URL; get_user_dashboard() answers it in its own
# branch, before the Site Dashboard check, so this entry is likewise never read.
ROLE_DASHBOARD = {
    'Admin':         SITE_DASHBOARD_URL,
    'System Admin':  SITE_DASHBOARD_URL,
    'PM':            SITE_DASHBOARD_URL,
    # Coordinators share the PM's home (scoped to their coordinated projects). Without
    # this entry the role fell back silently to the Admin dashboard — a
    # role-inappropriate-data-exposure risk, not a cosmetic bug.
    'Project Coordinator': SITE_DASHBOARD_URL,
    'Site Engineer': '/dashboard/site-engineer/',
    'Design':        '/dashboard/design/',
    'Finance':       FINANCE_WORK_URL,  # FIN-M-B8b: Finance lands on its own queue
    'SCM':           SITE_DASHBOARD_URL,
    'CEO':           SITE_DASHBOARD_URL,
    'BD':            '/dashboard/bd/',
}


# Roles that get the EPC Residential / Tenders context switcher (views._context_nav) on
# the old CEO, Finance and SCM dashboards. A display concern only — it grants and removes
# no access.
#
# DASH-B4: no longer a post-login destination. CEO and SCM land on the Site Dashboard like
# every other R1 role, Finance on the Finance work page (FIN-M-B8b), and /landing/ itself
# redirects each of them to that landing. The tuple
# stays because the CEO page's Residential and Tenders views are kept (ruling 5) and
# still draw the switcher, and because landing's @role_required reads it.
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

    # FIN-M-B8b: Finance lands on its own queue, the Finance work page; the Site Dashboard
    # is one click away from it. Decided by role alone (ruling 13 Nov): CEO, Admin and
    # System Admin may READ the Finance work page too, but their landing stays the Site
    # Dashboard, and no flag (is_payment_approver, is_design_head, ...) moves Finance
    # elsewhere. Asked of the page's own helper as well, so Home and every back link can
    # never point Finance at a page that refuses them. Before the Site Dashboard check
    # below, which also admits Finance. A context argument is not carried: the Finance
    # work page has no Residential / Tenders split.
    if role == 'Finance' and user_can_view_finance_work(user):
        return FINANCE_WORK_URL

    # DASH-B4 R1: PM, Project Coordinator, CEO, Finance, SCM, Admin, System Admin and the
    # Design Head belong on the Site Dashboard. Asked of the page's own access helper, so
    # Home, the 403 page's "Return to your dashboard" and every back link can never point
    # one of them at a page that refuses them, and the Design Head (a flag, not a role)
    # is caught where the role table above cannot see them. Every other role (Site
    # Engineer, designer, BD) keeps its own dashboard. Finance is answered above.
    if user_can_view_site_dashboard(user):
        return SITE_DASHBOARD_URL

    if context and role in LANDING_ROLES:
        return f'{url}?context={context}'
    return url


def get_post_login_url(user):
    """
    Where to send a user immediately after authenticating. Used by login_view, which
    also serves `/` through the /login/ redirect.

    DASH-B4 (R1): the role's own dashboard for everyone, which is the Site Dashboard for
    every role user_can_view_site_dashboard() admits except Finance, who land on the
    Finance work page (FIN-M-B8b). The CEO / Finance / SCM context chooser (/landing/) is
    no longer a stop on the way in.

    0.2 lockdown: profile-less resolves to NO_PROFILE_URL for the same reason
    get_user_dashboard() does. role_required() no longer redirects on denial at all — it
    returns 403 — so this helper is now only ever a post-login destination.
    """
    try:
        user.profile.role
    except Exception:
        logger.warning("No UserProfile for user %s — no post-login destination", user.username)
        return NO_PROFILE_URL
    return get_user_dashboard(user)


def retired_for_site_dashboard(serve_if=None):
    """
    DASH-B4 (R2): send a Site Dashboard user who opens a retired dashboard URL to their own
    landing page with a 302, so bookmarks, stored notification links and old back links
    keep working. Applied to dashboard_pm, dashboard_scm, dashboard_finance, dashboard_ceo
    and landing, ABOVE their @role_required and below @login_required:

        Site Dashboard roles (R1)  302 to get_user_dashboard(user), whichever old URL it
                                   is: SITE_DASHBOARD_URL for every R1 role but Finance,
                                   FINANCE_WORK_URL for Finance (FIN-M-B8b, option A)
        everyone else              falls through to @role_required: today's 403 unchanged

    The name is kept from DASH-B4: who is redirected is still decided by the Site
    Dashboard's helper; only where to is now each user's own landing.

    serve_if(request) -> True keeps the old page for that request. Only dashboard_ceo
    uses it: its Residential and Tenders views stay (ruling 5) and only the bare URL goes.

    THE SWITCH. settings.RETIRED_DASHBOARDS_REDIRECT, from the RETIRED_DASHBOARDS_REDIRECT
    environment variable (default True), so it can be turned off on Railway with no code
    change. The old views' own tests set it False (@override_settings) so they keep
    exercising the pages until the deletion commit removes those views, those tests and
    this switch together (ruling 3). Read per request, so override_settings takes effect.
    Turning it off restores the old pages only; login and Home still go to each user's
    landing (get_user_dashboard: the Site Dashboard, or Finance work for Finance).
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # Only a user the Site Dashboard admits is moved (the same helper the page
            # calls). Their target is get_user_dashboard(), which asks each landing page's
            # own helper, so the redirect never lands on a 403 and can never loop: neither
            # landing page carries this decorator. Anyone the Site Dashboard refuses keeps
            # whatever the old view's own decorator answers today.
            # getattr default True: a settings module without the line (a test settings
            # file that does not import settings.py) still redirects, as production does.
            if (getattr(settings, 'RETIRED_DASHBOARDS_REDIRECT', True)
                    and request.user.is_authenticated
                    and user_can_view_site_dashboard(request.user)
                    and not (serve_if is not None and serve_if(request))):
                return redirect(get_user_dashboard(request.user))
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def login_required(view_func):
    """Redirect unauthenticated users to /login/. Used in place of Django's built-in."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/login/')
        return view_func(request, *args, **kwargs)
    return wrapper


def _forbidden(request):
    """
    A 403 with a readable body.

    role_required() returns HttpResponseForbidden directly, which never reaches
    Django's permission_denied handler, so handler403/403.html would not be
    consulted — the template is rendered here explicitly.

    THE STATUS STAYS 403 (28 Aug, prompt 0.2): one authorisation failure, one
    response, so a probe cannot distinguish "wrong role" from "not yours". Only
    the empty body changes. 403.html discloses neither the requested URL, nor the
    role that would have been required, nor whether any object exists — the sole
    user-specific value passed to it is the caller's own dashboard URL, which
    they already know.
    """
    return HttpResponseForbidden(
        render(
            request,
            '403.html',
            {'dashboard_url': get_user_dashboard(request.user)},
        ).content
    )


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
                return _forbidden(request)
            if role not in allowed_roles:
                return _forbidden(request)
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


system_admin_required = role_required(['System Admin'])
