import importlib
from datetime import UTC, datetime

from django.conf import settings
from django.utils import timezone

import app.settings as app_settings


def test_eslog_ni_izpostavljen_v_v1():
    assert settings.FEATURE_ESLOG is False


def test_timezone_je_ljubljana_in_aware():
    assert settings.TIME_ZONE == "Europe/Ljubljana"
    assert settings.USE_TZ is True


def test_ljubljana_timezone_uposteva_dst():
    summer = timezone.localtime(datetime(2026, 10, 3, 10, 22, tzinfo=UTC))
    winter = timezone.localtime(datetime(2026, 1, 15, 11, 22, tzinfo=UTC))

    assert summer.isoformat() == "2026-10-03T12:22:00+02:00"
    assert winter.isoformat() == "2026-01-15T12:22:00+01:00"


def test_debug_je_privzeto_vklopljen(monkeypatch):
    monkeypatch.delenv("DJANGO_DEBUG", raising=False)

    assert importlib.reload(app_settings).DEBUG is True


def test_debug_lahko_izrecno_izklopimo(monkeypatch):
    monkeypatch.setenv("DJANGO_DEBUG", "0")
    try:
        assert importlib.reload(app_settings).DEBUG is False
    finally:
        monkeypatch.delenv("DJANGO_DEBUG")
        importlib.reload(app_settings)


def test_minimal_settings():
    assert settings.INSTALLED_APPS == [
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
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert settings.DATABASES["default"]["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
    assert [host for host in settings.ALLOWED_HOSTS if host != "testserver"] == [
        "127.0.0.1",
        "localhost",
    ]
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE
