from .decorators import get_user_dashboard
from .forms import TYPED_DATE_FLOOR, typed_date_ceiling
from .models import Notification
from .permissions import (
    user_can_manage_stock_locations, user_can_view_payment_queue,
    user_can_view_purchases_workspace, user_can_view_vendor_list,
)


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


def stock_location_nav(request):
    """Whether to show the Warehouses nav entry, in all three shells.

    The answer comes from permissions.user_can_manage_stock_locations() (R-13) so the
    link and the view can never disagree about who is let in. Visibility only — every
    warehouse view calls the helper itself.
    """
    if not request.user.is_authenticated:
        return {'can_manage_stock_locations': False}
    return {'can_manage_stock_locations': user_can_manage_stock_locations(request.user)}


def vendor_nav(request):
    """Whether to show the Vendors entry in the navbar's Masters menu.

    The answer comes from permissions.user_can_view_vendor_list() (R-13), the same
    helper vendor_list calls, so the link and the view cannot disagree. Visibility only.
    """
    if not request.user.is_authenticated:
        return {'can_view_vendor_list': False}
    return {'can_view_vendor_list': user_can_view_vendor_list(request.user)}


def payment_queue_nav(request):
    """Whether to show the Payments nav entry (O5).

    The answer comes from permissions.user_can_view_payment_queue() (R-13), the same
    helper payment_queue calls, so the link and the view cannot disagree. Visibility only.
    """
    if not request.user.is_authenticated:
        return {'can_view_payment_queue': False}
    return {'can_view_payment_queue': user_can_view_payment_queue(request.user)}


def purchases_nav(request):
    """Whether to show the Purchases & payments nav entry (O8a).

    The answer comes from permissions.user_can_view_purchases_workspace() (R-13), the
    same helper purchases_workspace calls, so the link and the view cannot disagree.
    """
    if not request.user.is_authenticated:
        return {'can_view_purchases_workspace': False}
    return {'can_view_purchases_workspace': user_can_view_purchases_workspace(request.user)}


def typed_date_bounds(request):
    """min/max for every <input type="date"> that stores a typed date.

    The server-side rule is forms.check_typed_date(); these two values let the picker
    say the same thing before the POST. The ceiling moves with the calendar, so it is
    computed per request, not hard-coded in a template.
    """
    return {
        'typed_date_min': TYPED_DATE_FLOOR.isoformat(),
        'typed_date_max': typed_date_ceiling().isoformat(),
    }
