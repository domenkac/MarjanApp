import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent

# ponytail: restart changes this key; set DJANGO_SECRET_KEY if sessions are added.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = ["django.contrib.contenttypes", "core", "companies", "catalogs", "contracts", "invoicing", "rendering"]
MIDDLEWARE = ["django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "app.urls"

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
