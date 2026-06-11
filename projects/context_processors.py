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
