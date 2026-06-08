import logging
from django.shortcuts import redirect

logger = logging.getLogger(__name__)

ROLE_DASHBOARD = {
    'Admin':         '/dashboard/admin/',
    'PM':            '/dashboard/pm/',
    'Site Engineer': '/dashboard/site-engineer/',
    'Design':        '/dashboard/design/',
    'Finance':       '/dashboard/finance/',
    'SCM':           '/dashboard/scm/',
    'CEO':           '/dashboard/ceo/',
}


class AdminAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/admin/') and request.user.is_authenticated:
            try:
                role = request.user.profile.role
            except Exception:
                logger.warning("No UserProfile for %s in AdminAccessMiddleware", request.user.username)
                role = 'Admin'

            if role != 'Admin':
                return redirect(ROLE_DASHBOARD.get(role, '/dashboard/admin/'))

        return self.get_response(request)
