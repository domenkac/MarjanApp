import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent

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
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        "TEST": {"NAME": BASE_DIR / "test.sqlite3"},
    }
}

PDF_TEMPLATE_VERSION = "1"
RENDERING_ROOT = BASE_DIR / "rendered"
FEATURE_ESLOG = False
STATIC_URL = "static/"
