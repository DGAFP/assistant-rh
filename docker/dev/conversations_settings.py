"""Single-process, loopback-only developer configuration for the pinned fork."""

from conversations.settings import Development


class LocalDev(Development):
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "conversations"]
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    SESSION_ENGINE = "django.contrib.sessions.backends.db"
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = []
    CSRF_TRUSTED_ORIGINS = []
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    # File uploads are disabled in RH mode; no object store is needed locally.
    AWS_S3_ACCESS_KEY_ID = "local-unused"
    AWS_S3_SECRET_ACCESS_KEY = "local-unused"
    FRONTEND_HOMEPAGE_FEATURE_ENABLED = False
