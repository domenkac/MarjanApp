from django.conf import settings


def test_eslog_ni_izpostavljen_v_v1():
    assert settings.FEATURE_ESLOG is False


def test_minimal_settings():
    assert settings.INSTALLED_APPS == [
        "django.contrib.contenttypes",
        "core",
        "companies",
        "catalogs",
        "contracts",
        "invoicing",
        "rendering",
    ]
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert settings.DATABASES["default"]["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
    assert [host for host in settings.ALLOWED_HOSTS if host != "testserver"] == [
        "127.0.0.1",
        "localhost",
    ]
    assert "django.middleware.csrf.CsrfViewMiddleware" in settings.MIDDLEWARE
