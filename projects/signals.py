import logging
from django.db import transaction
from django.db.models.signals import post_save
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import UserProfile, ActivityLog, StatusTransition, SUBJECT_PAYMENT_REQUEST

logger = logging.getLogger(__name__)


@receiver(post_save, sender=StatusTransition)
def payment_transition_notices(sender, instance, created, **kwargs):
    """O5: every payment_request ledger row is a move someone may need to hear about.

    HOOKED ON THE LEDGER, NOT ON THE VIEWS. A payment's status only moves where
    record_transition() writes its row (execution-model.md §13 lists the six sites), so
    this one receiver covers the raise, the hold, SCM's answer and mark-paid without a
    line changed in the four O4 action views. payments.send_payment_notices() decides
    who hears about which move.

    AFTER COMMIT. The row is written inside the move's transaction; on_commit defers the
    notice until that transaction commits, so a move that rolls back — a refusal inside
    the lock, a lost race — tells nobody. An idempotent replay of record_transition()
    returns the existing row without saving, so it notifies nobody twice.

    SUPPRESSIBLE (O6). record_transition(..., notify=False) sets `_notify = False` on the
    row before saving it, and this receiver then sends nothing. The flag lives on the
    Python object, not in a column: post_save hands this function the same instance
    record_transition() built, and nothing needs it afterwards. A row saved any other way
    has no `_notify` and notifies as before. THIS IS THE ONE SANCTIONED EXCEPTION to
    "every send is a manual call site" (docs/execution-model.md §12, 24 Sep): a bulk or
    corrective script that records payment moves MUST pass notify=False, or every row it
    writes notifies real approvers and requesters.
    """
    if not created or instance.subject_type != SUBJECT_PAYMENT_REQUEST:
        return
    if not getattr(instance, '_notify', True):
        return
    from .payments import send_payment_notices
    transaction.on_commit(lambda: send_payment_notices(instance))


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
            action_code='user_login',   # machine-readable key so digests can exclude auth noise
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
                action_code='user_logout',   # machine-readable key so digests can exclude auth noise
                entity_type='User',
                entity_id=user.id,
            )
    except Exception:
        pass
