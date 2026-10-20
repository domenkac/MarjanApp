"""Explicit desktop settings; loaded only by the dedicated launcher."""

from app.settings import *
from desktop.profile import default_profile

_profile = default_profile()
SECRET_KEY = _profile.load()
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1"]
MARJAN_DESKTOP_TEST = True
MARJAN_TEST_ARCHIVE_PATHS = _profile.archive_paths
MARJAN_SMTP_HOST = ""
MARJAN_SMTP_PORT = ""
MARJAN_SMTP_SECURITY = ""
MARJAN_SMTP_TIMEOUT_SECONDS = ""
MARJAN_KEYRING_SERVICE = ""
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
MEDIA_ROOT = _profile.root / "media"
RENDERING_ROOT = _profile.root / "rendered"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": _profile.database,
    "OPTIONS": {"transaction_mode": "IMMEDIATE", "factory": partial(
        existing_database_connection, database_path=_profile.database,
    )},
}}
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_HTTPONLY = True
CSRF_FAILURE_VIEW = "desktop.gateway.csrf_failure"
# Never include application exceptions or request paths (which carry a capability).
LOGGING_CONFIG = None
LOGGING = {}
TEMPLATES = [{**TEMPLATES[0], "OPTIONS": {"context_processors": ["desktop.context.profile_label"]}}]
