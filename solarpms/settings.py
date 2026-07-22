from pathlib import Path
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Security — all sourced from environment variables via python-decouple
# ---------------------------------------------------------------------------

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)

# ALLOWED_HOSTS must include the Railway public domain in production
# e.g. ALLOWED_HOSTS=your-app.up.railway.app,localhost
ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

# Alphanumeric token checked on every incoming Zoho webhook request
ZOHO_WEBHOOK_SECRET = config('ZOHO_WEBHOOK_SECRET', default='')

# ---------------------------------------------------------------------------
# Supabase file storage
# ---------------------------------------------------------------------------

SUPABASE_URL    = config('SUPABASE_URL', default='')
SUPABASE_KEY    = config('SUPABASE_KEY', default='')
SUPABASE_BUCKET = config('SUPABASE_BUCKET', default='solarpms-files')  # Bucket for all project/task files

# How long (days) to keep soft-deleted files before purge_deleted_files hard-deletes them
FILE_RETENTION_DAYS        = config('FILE_RETENTION_DAYS', default=90, cast=int)

# Raised from Django default (2.5 MB) to support multi-file uploads up to 20 MB each
DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100 MB

# ---------------------------------------------------------------------------
# CSRF — must include the Railway domain so POST forms are not rejected
# ---------------------------------------------------------------------------

# e.g. CSRF_TRUSTED_ORIGINS=https://your-app.up.railway.app,http://localhost
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='http://localhost').split(',')

# Required for Railway (and most reverse-proxy setups) so Django sees HTTPS correctly
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',  # Serves static files in development too — avoids STATIC_URL 404s
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'projects',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Must come immediately after SecurityMiddleware
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'solarpms.middleware.AdminAccessMiddleware',  # Custom: restricts /admin/ to staff users
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'solarpms.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'projects.context_processors.notifications',  # Injects unread_count into every template
            ],
        },
    },
]

WSGI_APPLICATION = 'solarpms.wsgi.application'

# ---------------------------------------------------------------------------
# Database — URL-based config; Railway injects DATABASE_URL automatically
# ---------------------------------------------------------------------------

DATABASES = {
    'default': dj_database_url.config(
        default=config('DATABASE_URL')
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'  # IST — all datetimes stored as UTC, displayed as IST
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files — WhiteNoise serves compressed, cached static assets
# ---------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------

# Interakt (WhatsApp via Meta Business API)
# Set in Railway env vars; leave blank locally — send_notification() logs 'failed' gracefully.
INTERAKT_API_KEY = config('INTERAKT_API_KEY', default='')

# ZeptoMail (transactional email — Zoho)
# Domain verification (SPF + DKIM) must complete before any email will send.
ZEPTOMAIL_API_KEY    = config('ZEPTOMAIL_API_KEY', default='')
ZEPTOMAIL_FROM_EMAIL = config('ZEPTOMAIL_FROM_EMAIL', default='noreply@horizonrenewablepower.com')

# EOD aggregate (company-wide) digest recipients — fixed addresses, not UserProfiles.
# Mirrors the Santosh-for-Finance hardcoding pattern. Zuber MUST replace both
# placeholders (env vars ADMIN_DIGEST_EMAIL / HR_DIGEST_EMAIL, or edit here) with real
# addresses before production; send_eod_digest refuses the aggregate send while either
# still contains "REPLACE_WITH". For NotificationLog to record these sends, the address
# should match a real user account's email (see notifications.send_aggregate_email).
ADMIN_DIGEST_EMAIL = config('ADMIN_DIGEST_EMAIL', default='REPLACE_WITH_ACTUAL_ADMIN_EMAIL')
HR_DIGEST_EMAIL    = config('HR_DIGEST_EMAIL',    default='REPLACE_WITH_ACTUAL_HR_EMAIL')

# EOD individual-digest hard role exclusions (UserProfile.role stored values). These roles
# never receive a personal digest regardless of activity — they already get the company-wide
# aggregate email above, so a personal digest is redundant (NOT an activity/low-involvement
# decision). Values must match UserProfile.ROLE_CHOICES exactly. 'BD' is deliberately absent —
# BD is handled by the open-work gating in send_eod_digest, not a hard exclusion.
EOD_DIGEST_EXCLUDED_ROLES = ['CEO', 'Admin', 'System Admin']

# SendGrid fallback — uncomment in notifications.py _send_email() if ZeptoMail stalls
# SENDGRID_API_KEY = config('SENDGRID_API_KEY', default='')

# ---------------------------------------------------------------------------
# Auth redirects
# ---------------------------------------------------------------------------

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/admin/'
LOGOUT_REDIRECT_URL = '/login/'

# ---------------------------------------------------------------------------
# Messages — map Django's ERROR level to Bootstrap's 'danger' CSS class
# ---------------------------------------------------------------------------

from django.contrib.messages import constants as message_constants
MESSAGE_TAGS = {
    message_constants.ERROR: 'danger',
}

# ---------------------------------------------------------------------------
# Logging — explicit config required; without it Zoho webhook errors are
# swallowed silently (discovered in production debugging)
# ---------------------------------------------------------------------------

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}

ADMIN_DIGEST_EMAIL = 'smzk07@gmail.com'
HR_DIGEST_EMAIL = 'shweta@horizonrenewablepower.com'