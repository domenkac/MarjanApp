import runpy
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.db.utils import ConnectionHandler

SETTINGS_FILE = Path(__file__).resolve().parents[1] / "app" / "settings.py"


@pytest.fixture
def load_settings(monkeypatch, tmp_path):
    for name in ("MARJAN_DB_PATH", "MARJAN_RENDERING_ROOT", "MARJAN_MEDIA_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return lambda: runpy.run_path(str(SETTINGS_FILE))


@pytest.fixture
def open_database(django_db_blocker):
    opened = []

    def open_configured(configuration):
        wrapper = ConnectionHandler({"default": configuration})["default"]
        opened.append(wrapper)
        wrapper.ensure_connection()
        return wrapper

    with django_db_blocker.unblock():
        try:
            yield open_configured
        finally:
            for wrapper in opened:
                wrapper.close()


def test_development_defaults_are_unchanged(load_settings):
    configured = load_settings()
    base = configured["BASE_DIR"]

    assert configured["DATABASES"]["default"] == {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": base / "db.sqlite3",
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        "TEST": {"NAME": base / "test.sqlite3"},
    }
    assert configured["RENDERING_ROOT"] == base / "rendered"
    assert configured["MEDIA_ROOT"] == (base / "media").resolve()


def test_explicit_paths_resolve_without_creating_data(
    load_settings, monkeypatch, tmp_path
):
    for name, leaf in (
        ("MARJAN_DB_PATH", "data.sqlite3"),
        ("MARJAN_RENDERING_ROOT", "rendered"),
        ("MARJAN_MEDIA_ROOT", "media"),
    ):
        monkeypatch.setenv(name, str(tmp_path / "unused" / ".." / leaf))

    configured = load_settings()

    assert configured["DATABASES"]["default"]["NAME"] == tmp_path / "data.sqlite3"
    assert configured["RENDERING_ROOT"] == tmp_path / "rendered"
    assert configured["MEDIA_ROOT"] == tmp_path / "media"
    assert list(tmp_path.iterdir()) == []


def test_all_paths_expand_home(load_settings, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MARJAN_DB_PATH", "~/MarjanApp/data.sqlite3")
    monkeypatch.setenv("MARJAN_RENDERING_ROOT", "~/MarjanApp/rendered")
    monkeypatch.setenv("MARJAN_MEDIA_ROOT", "~/MarjanApp/media")

    configured = load_settings()

    assert configured["DATABASES"]["default"]["NAME"] == (
        tmp_path / "MarjanApp" / "data.sqlite3"
    )
    assert configured["RENDERING_ROOT"] == tmp_path / "MarjanApp" / "rendered"
    assert configured["MEDIA_ROOT"] == tmp_path / "MarjanApp" / "media"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["MARJAN_DB_PATH", "MARJAN_RENDERING_ROOT"])
@pytest.mark.parametrize(
    "value", ["", " ", "relative/path", "C:relative.sqlite3", ":memory:", "file:data"]
)
def test_new_overrides_require_absolute_filesystem_paths(
    load_settings, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ImproperlyConfigured, match=name):
        load_settings()


def test_database_and_documents_survive_reloading_settings(
    load_settings, monkeypatch, tmp_path, open_database, settings
):
    database = tmp_path / "Marjan podatki č #%.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('retained')")
        connection.commit()
    original = database.read_bytes()
    root = tmp_path / "rendered"
    (root / "company-12").mkdir(parents=True)
    document = root / "company-12" / "NP-00001.pdf"
    document.write_bytes(b"existing temporary document")
    monkeypatch.setenv("MARJAN_DB_PATH", str(database))
    monkeypatch.setenv("MARJAN_RENDERING_ROOT", str(root))
    updated_settings = tmp_path / "updated-source" / "app" / "settings.py"
    updated_settings.parent.mkdir(parents=True)
    updated_settings.write_bytes(SETTINGS_FILE.read_bytes())

    for settings_file in (SETTINGS_FILE, SETTINGS_FILE, updated_settings):
        configured = runpy.run_path(str(settings_file))
        wrapper = open_database(configured["DATABASES"]["default"])
        assert wrapper.settings_dict["NAME"] == database
        with wrapper.cursor() as cursor:
            cursor.execute("SELECT value FROM evidence")
            assert cursor.fetchall() == [("retained",)]
        wrapper.close()
        settings.RENDERING_ROOT = configured["RENDERING_ROOT"]
        from rendering.paths import varna_render_pot_podjetja

        relative, resolved = varna_render_pot_podjetja(12, "NP-00001.pdf")
        assert relative == "company-12/NP-00001.pdf"
        assert resolved == document
        assert resolved.read_bytes() == b"existing temporary document"

    assert database.read_bytes() == original


@pytest.mark.parametrize("state", ["missing", "missing-parent", "directory", "removed"])
def test_unavailable_database_is_never_created(
    load_settings, monkeypatch, tmp_path, open_database, state
):
    database = tmp_path / "data.sqlite3"
    if state == "missing-parent":
        database = tmp_path / "unavailable" / "data.sqlite3"
    elif state == "directory":
        database.mkdir()
    elif state == "removed":
        with closing(sqlite3.connect(database)):
            pass
    monkeypatch.setenv("MARJAN_DB_PATH", str(database))
    configured = load_settings()
    if state == "removed":
        database.unlink()

    with pytest.raises(OperationalError):
        open_database(configured["DATABASES"]["default"])

    if state == "directory":
        assert list(database.iterdir()) == []
    else:
        assert not database.exists()
    assert not (tmp_path / "unavailable").exists()


def test_reconnecting_does_not_recreate_removed_database(
    load_settings, monkeypatch, tmp_path, open_database, django_db_blocker
):
    database = tmp_path / "data.sqlite3"
    with closing(sqlite3.connect(database)):
        pass
    monkeypatch.setenv("MARJAN_DB_PATH", str(database))
    wrapper = open_database(load_settings()["DATABASES"]["default"])
    wrapper.close()
    database.unlink()

    with django_db_blocker.unblock(), pytest.raises(OperationalError):
        wrapper.ensure_connection()

    assert not database.exists()


def test_configured_database_keeps_immediate_transactions(
    load_settings, monkeypatch, tmp_path, open_database
):
    database = tmp_path / "data.sqlite3"
    with closing(sqlite3.connect(database)):
        pass
    monkeypatch.setenv("MARJAN_DB_PATH", str(database))
    wrapper = open_database(load_settings()["DATABASES"]["default"])
    statements = []
    wrapper.connection.set_trace_callback(statements.append)

    wrapper.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
    wrapper.rollback()
    wrapper.set_autocommit(True)

    assert "BEGIN IMMEDIATE" in statements


def test_separate_test_database_can_still_be_created(
    load_settings, monkeypatch, tmp_path, open_database
):
    monkeypatch.setenv("MARJAN_DB_PATH", str(tmp_path / "unavailable-live.sqlite3"))
    configuration = load_settings()["DATABASES"]["default"]
    configuration["NAME"] = tmp_path / "isolated-test.sqlite3"

    wrapper = open_database(configuration)

    assert wrapper.settings_dict["NAME"].is_file()
    assert not (tmp_path / "unavailable-live.sqlite3").exists()
