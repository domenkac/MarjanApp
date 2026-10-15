import os
from functools import partial
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

from app.sqlite import existing_database_connection


def _persistent_path(name, default):
    if name not in os.environ:
        return default
    try:
        value = os.environ[name]
        path = Path(value).expanduser()
        if not value.strip() or not path.is_absolute():
            raise ValueError
        return path.resolve()
    except (ValueError, OSError, RuntimeError) as error:
        raise ImproperlyConfigured(
            f"{name} must be an absolute filesystem path (or start with ~/)."
        ) from error


BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_ROOT = Path(
    os.environ.get("MARJAN_MEDIA_ROOT", BASE_DIR / "media")
).expanduser().resolve()

# ponytail: restart changes this key; set DJANGO_SECRET_KEY if sessions are added.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
DEBUG = os.environ.get("DJANGO_DEBUG", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
TIME_ZONE = "Europe/Ljubljana"
USE_TZ = True

# Empty defaults fail closed in delivery.configuration before any network access.
MARJAN_SMTP_HOST = os.environ.get("MARJAN_SMTP_HOST", "")
MARJAN_SMTP_PORT = os.environ.get("MARJAN_SMTP_PORT", "")
MARJAN_SMTP_SECURITY = os.environ.get("MARJAN_SMTP_SECURITY", "")
MARJAN_SMTP_TIMEOUT_SECONDS = os.environ.get("MARJAN_SMTP_TIMEOUT_SECONDS", "")
MARJAN_KEYRING_SERVICE = os.environ.get("MARJAN_KEYRING_SERVICE", "")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
    "companies",
    "catalogs",
    "contracts",
    "invoicing",
    "delivery",
    "rendering",
    "archive",
    "accounting_export",
    "ui",
]
MIDDLEWARE = ["django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "app.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _persistent_path("MARJAN_DB_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        "TEST": {"NAME": BASE_DIR / "test.sqlite3"},
    }
}
if "MARJAN_DB_PATH" in os.environ:
    # Keep NAME as a filesystem path for backup, restore, and lock safeguards.
    DATABASES["default"]["OPTIONS"]["factory"] = partial(
        existing_database_connection,
        database_path=DATABASES["default"]["NAME"],
    )

PDF_TEMPLATE_VERSION = "1"
RENDERING_ROOT = _persistent_path("MARJAN_RENDERING_ROOT", BASE_DIR / "rendered")
FEATURE_ESLOG = False
STATIC_URL = "static/"
