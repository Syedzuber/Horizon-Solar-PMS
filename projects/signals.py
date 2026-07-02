import logging
from django.db.models.signals import post_save
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import UserProfile, ActivityLog

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        try:
            UserProfile.objects.get_or_create(user=instance)
        except Exception as e:
            logger.warning("Could not create UserProfile for user %s: %s", instance.username, e)


def _get_ip(request):
    """Extract real IP, respecting Railway's proxy headers."""
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    try:
        profile = user.profile  # related_name='profile' on UserProfile.user
        ip = _get_ip(request)
        ActivityLog.objects.create(
            project=None,
            actor=profile,
            action=f"User logged in from {ip}",
            entity_type='User',
            entity_id=user.id,
        )
    except Exception:
        pass


@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    try:
        if user and hasattr(user, 'profile'):
            ActivityLog.objects.create(
                project=None,
                actor=user.profile,
                action="User logged out",
                entity_type='User',
                entity_id=user.id,
            )
    except Exception:
        pass
