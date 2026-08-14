from .decorators import get_user_dashboard
from .models import Notification


def notifications(request):
    if not request.user.is_authenticated:
        return {'unread_notification_count': 0}
    try:
        count = Notification.objects.filter(
            recipient=request.user.profile,
            is_read=False,
        ).count()
    except Exception:
        count = 0
    return {'unread_notification_count': count}


def home_url(request):
    """
    Every authenticated user's own dashboard URL, for the Home control in the
    nav. Exists because templates had no way to ask "where does this user
    belong?" and were hardcoding /dashboard/admin/, which is the wrong
    destination for every role except Admin.

    get_user_dashboard() is the single resolution authority — do not read
    ROLE_DASHBOARD here or branch on a role string. Anonymous requests get None
    so the login page renders without a profile lookup.
    """
    if not request.user.is_authenticated:
        return {'home_url': None}
    return {'home_url': get_user_dashboard(request.user)}
