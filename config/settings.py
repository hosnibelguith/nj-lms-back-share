"""
Django settings for LendStack project.
Configured for local development + Heroku deployment.
"""

import os
from pathlib import Path
from datetime import timedelta

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def env_list(var_name, default=""):
    value = os.environ.get(var_name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


# -------------------------------------------------------------------
# Core settings
# -------------------------------------------------------------------
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1,.herokuapp.com"
)

if not DEBUG and SECRET_KEY == "dev-secret-key-change-me":
    raise ValueError("DJANGO_SECRET_KEY must be set in production.")


# -------------------------------------------------------------------
# Security / proxy
# -------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False


# -------------------------------------------------------------------
# Application definition
# -------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party apps
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "django_celery_beat",
    "django_celery_results",

    # Local apps
    "accounts",
    "banking",
    "loans",
    "contracts",
    "communications",
    "activity",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# -------------------------------------------------------------------
# Database
# Local: SQLite
# Production/Heroku: PostgreSQL via DATABASE_URL
# -------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

if "DATABASE_URL" in os.environ:
    DATABASES["default"] = dj_database_url.config(
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=not DEBUG,
    )


# -------------------------------------------------------------------
# Custom user model
# -------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"


# -------------------------------------------------------------------
# Password validation
# -------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# -------------------------------------------------------------------
# Internationalization
# -------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Toronto"
USE_I18N = True
USE_TZ = True


# -------------------------------------------------------------------
# Static files
# -------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}


# -------------------------------------------------------------------
# Media files
# -------------------------------------------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# -------------------------------------------------------------------
# Default primary key field type
# -------------------------------------------------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -------------------------------------------------------------------
# Django REST Framework
# -------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.CookieJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "50000/day" if DEBUG else "50000/hour",
        "user": "50000/day" if DEBUG else "50000/hour",
    },
}


# -------------------------------------------------------------------
# JWT Settings
# -------------------------------------------------------------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# -------------------------------------------------------------------
# CORS / CSRF
# -------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000"
)
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000"
)


# -------------------------------------------------------------------
# Celery
# -------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get(
    "REDIS_URL",
    os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
)
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "django-db")

if CELERY_BROKER_URL and CELERY_BROKER_URL.startswith("rediss://") and "ssl_cert_reqs=" not in CELERY_BROKER_URL:
    CELERY_BROKER_URL += "?ssl_cert_reqs=none"

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "False").lower() == "true"
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER


# -------------------------------------------------------------------
# Email
# -------------------------------------------------------------------
# -------------------------------------------------------------------
# Email / SMTP
# -------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@lendstack.com")
INBOUND_EMAIL_PROVIDER = os.environ.get("INBOUND_EMAIL_PROVIDER", "graph")
INBOUND_EMAIL_HOST = os.environ.get("INBOUND_EMAIL_HOST", "outlook.office365.com")
INBOUND_EMAIL_PORT = int(os.environ.get("INBOUND_EMAIL_PORT", "993"))
INBOUND_EMAIL_USER = os.environ.get("INBOUND_EMAIL_USER", EMAIL_HOST_USER)
INBOUND_EMAIL_PASSWORD = os.environ.get("INBOUND_EMAIL_PASSWORD", EMAIL_HOST_PASSWORD)
INBOUND_EMAIL_MAILBOX = os.environ.get("INBOUND_EMAIL_MAILBOX", "INBOX")
INBOUND_EMAIL_POLL_ENABLED = os.environ.get("INBOUND_EMAIL_POLL_ENABLED", "False").lower() == "true"
INBOUND_EMAIL_POLL_LIMIT = int(os.environ.get("INBOUND_EMAIL_POLL_LIMIT", "50"))
GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
GRAPH_MAILBOX = os.environ.get("GRAPH_MAILBOX", INBOUND_EMAIL_USER)
# -------------------------------------------------------------------
# Twilio
# -------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
DEV_OTP_CODE = os.environ.get("DEV_OTP_CODE", "123456" if DEBUG else "")


# -------------------------------------------------------------------
# Flinks
# -------------------------------------------------------------------
FLINKS_IFRAME_URL = os.environ.get(
    "FLINKS_IFRAME_URL",
    "https://alphaloans-ca-iframe.private.fin.ag/v2/?demo=false&tag=lendstackdemo",
)
FLINKS_INSTANCE = os.environ.get("FLINKS_INSTANCE", "alphaloans-ca")
FLINKS_CUSTOMER_ID = os.environ.get("FLINKS_CUSTOMER_ID", "855a21f3-976d-430f-9162-f5d2254b0bad")
FLINKS_SECRET_KEY_CA = os.environ.get("FLINKS_SECRET_KEY_CA", "")
MOHAWK_BANKING_ANALYSIS_API_KEY = os.environ.get("MOHAWK_BANKING_ANALYSIS_API_KEY", "")


# -------------------------------------------------------------------
# ZūmRails
# -------------------------------------------------------------------
ZUMRAILS_API_BASE_URL = os.environ.get("ZUMRAILS_API_BASE_URL", "")
ZUMRAILS_API_KEY = os.environ.get("ZUMRAILS_API_KEY", "")
ZUMRAILS_WEBHOOK_SECRET = os.environ.get("ZUMRAILS_WEBHOOK_SECRET", "")
ZUMRAILS_DRY_RUN = os.environ.get("ZUMRAILS_DRY_RUN", "True" if DEBUG else "False").lower() == "true"


# -------------------------------------------------------------------
# AWS S3
# -------------------------------------------------------------------
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "lendstack-files")
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "ca-central-1")


ARRIVE_API_KEY = os.environ.get("ARRIVE_API_KEY", "")
ARRIVE_WEBHOOK_URL = os.environ.get(
    "ARRIVE_WEBHOOK_URL",
    "https://app.arrivecard.ca/api/webhooks/lendstack/decision/",
)
ARRIVE_WEBHOOK_SECRET = os.environ.get("ARRIVE_WEBHOOK_SECRET", "")
ARRIVE_PORTAL_BASE_URL = os.environ.get(
    "ARRIVE_PORTAL_BASE_URL",
    os.environ.get("FRONTEND_URL", "http://localhost:3000"),
)
ARRIVE_FRAME_ANCESTORS = env_list(
    "ARRIVE_FRAME_ANCESTORS",
    "https://app.arrivecard.ca",
)
ARRIVE_HANDOFF_TOKEN_TTL_SECONDS = int(os.environ.get("ARRIVE_HANDOFF_TOKEN_TTL_SECONDS", "1800"))


# -------------------------------------------------------------------
# Frontend URL
# -------------------------------------------------------------------
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "[{asctime}] {levelname} {message}",
            "style": "{",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "banking": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "loans": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


# -------------------------------------------------------------------
# Cookie auth settings
# -------------------------------------------------------------------
AUTH_COOKIE_ACCESS = "access_token"
AUTH_COOKIE_REFRESH = "refresh_token"

AUTH_COOKIE_HTTP_ONLY = True
if DEBUG:
    AUTH_COOKIE_SECURE = False
    AUTH_COOKIE_SAMESITE = "Lax"
else:
    AUTH_COOKIE_SECURE = True
    AUTH_COOKIE_SAMESITE = "None"
AUTH_COOKIE_ACCESS_PATH = "/"
AUTH_COOKIE_REFRESH_PATH = "/"
AUTH_COOKIE_MAX_AGE_ACCESS = 60 * 60            # 1 hour
AUTH_COOKIE_MAX_AGE_REFRESH = 60 * 60 * 24 * 7  # 7 days
CSRF_COOKIE_NAME = "csrftoken"
CSRF_COOKIE_PATH = "/"
CSRF_COOKIE_HTTPONLY = False

if DEBUG:
    CSRF_COOKIE_SAMESITE = "Lax"
else:
    CSRF_COOKIE_SAMESITE = "None"
