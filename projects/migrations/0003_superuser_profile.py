import logging
from django.db import migrations

logger = logging.getLogger(__name__)


def create_superuser_profiles(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    UserProfile = apps.get_model('projects', 'UserProfile')

    superusers = User.objects.filter(is_superuser=True)
    for user in superusers:
        profile, created = UserProfile.objects.get_or_create(user=user)
        if created or not profile.role:
            profile.role = 'Admin'
            profile.save()
            logger.info("Created Admin UserProfile for superuser: %s", user.username)


def reverse_superuser_profiles(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0002_userprofile'),
    ]

    operations = [
        migrations.RunPython(create_superuser_profiles, reverse_superuser_profiles),
    ]
