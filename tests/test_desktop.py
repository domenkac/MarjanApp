import json
import os
import runpy
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from desktop import launcher
from desktop.gateway import Gateway
from desktop.profile import (
    DesktopError,
    ProfileLock,
    TestProfile,
    configure_environment,
    default_profile,
    new_secret,
    write_private,
)

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile(tmp_path):
    result = TestProfile(tmp_path / "TEST")
    for directory in result.directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(result.database)) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('synthetic untouched')")
        db.commit()
    result.database.chmod(0o600)
    info = result.database.stat()
    write_private(result.root / "profile.json", json.dumps({
        "profile": "TEST", "version": 1, "database_id": [info.st_dev, info.st_ino],
    }))
    write_private(result.root / "secret.key", new_secret())
    return result


def test_missing_installation_never_creates_paths(tmp_path):
    profile = TestProfile(tmp_path / "absent")
    with pytest.raises(DesktopError, match="ni pripravljena"):
        profile.load()
    assert not profile.root.exists()


def test_missing_database_fails_closed(profile):
    profile.database.unlink()
    with pytest.raises(DesktopError):
        profile.load()
    assert not profile.database.exists()


def test_secret_is_stable_and_setup_does_not_modify_database(profile, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    original = profile.database.read_bytes()
    secret = profile.load()
    for _ in range(2):
        configure_environment(profile)
        assert os.environ["DJANGO_SECRET_KEY"] == secret
    assert profile.database.read_bytes() == original


@pytest.mark.parametrize("name", ["MARJAN_DB_PATH", "MARJAN_MEDIA_ROOT", "MARJAN_RENDERING_ROOT", "MARJAN_SMTP_HOST", "MARJAN_KEYRING_SERVICE", "DJANGO_SETTINGS_MODULE", "DJANGO_DEBUG", "DJANGO_SECRET_KEY"])
def test_inherited_production_settings_cannot_select_data(profile, monkeypatch, name):
    monkeypatch.setenv(name, "production")
    before = {k: v for k, v in os.environ.items() if k.startswith(("MARJAN_", "DJANGO_"))}
    try:
        configure_environment(profile)
        assert os.environ.get(name) != "production"
        assert os.environ["MARJAN_DB_PATH"] == str(profile.database)
        assert os.environ["DJANGO_SETTINGS_MODULE"] == "desktop.settings"
        assert os.environ["DJANGO_DEBUG"] == "0"
    finally:
        for key in tuple(os.environ):
            if key.startswith(("MARJAN_", "DJANGO_")):
                del os.environ[key]
        os.environ.update(before)


def test_fixed_profile_has_no_production_selector(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MARJAN_DESKTOP_PROFILE", "production")
    assert default_profile().root == tmp_path / "Library" / "Application Support" / "MarjanApp-TEST"


@pytest.mark.parametrize("file", ["secret.key", "profile.json"])
def test_corrupt_private_configuration_is_rejected(profile, file):
    (profile.root / file).write_text("invalid", encoding="utf-8")
    with pytest.raises(DesktopError):
        profile.load()


def test_replaced_database_is_not_activated(profile):
    replacement = profile.root / "replacement.sqlite3"
    replacement.write_bytes(profile.database.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, profile.database)
    with pytest.raises(DesktopError):
        profile.load()


def test_hardlinked_database_rejected(profile):
    os.link(profile.database, profile.root / "other.sqlite3")
    with pytest.raises(DesktopError):
        profile.load()


def test_duplicate_launcher_and_lock_release(profile):
    with ProfileLock(profile), pytest.raises(DesktopError, match="že deluje"), ProfileLock(profile):
        pytest.fail("Duplicate launcher acquired lock")
    with ProfileLock(profile):
        pass
    assert (profile.root / "desktop.lock").exists()


def test_lock_is_enforced_across_processes(profile):
    with ProfileLock(profile):
        result = subprocess.run([
            sys.executable, "-c",
            ("import sys; from pathlib import Path; from desktop.profile import TestProfile, ProfileLock; "
             "ProfileLock(TestProfile(Path(sys.argv[1]))).__enter__()"), str(profile.root),
        ], cwd=REPOSITORY, capture_output=True, timeout=10, check=False)
    assert result.returncode != 0
    assert b"DesktopError" in result.stderr


@pytest.mark.parametrize("overrides", [
    {"HTTP_HOST": "evil.example"}, {"HTTP_HOST": "127.0.0.1:2"},
    {"HTTP_ORIGIN": "https://evil.example"}, {"HTTP_ORIGIN": "null"},
    {"HTTP_SEC_FETCH_SITE": "same-site"}, {"HTTP_SEC_FETCH_SITE": "cross-site"},
    {"REMOTE_ADDR": "192.0.2.1"}, {"PATH_INFO": "/"},
    {"PATH_INFO": "/_desktop/wrong/"}, {"PATH_INFO": "/_desktop/token-extra/"},
    {"PATH_INFO": "/_desktop/žeton/"},
])
def test_gateway_rejects_unauthorized_requests(overrides):
    application = Mock()
    gateway = Gateway(application, "/_desktop/token", "127.0.0.1:1234")
    response = Mock()
    assert gateway({
        "HTTP_HOST": "127.0.0.1:1234", "REMOTE_ADDR": "127.0.0.1",
        "PATH_INFO": "/_desktop/token/", **overrides,
    }, response) == [b"Request denied."]
    assert response.call_args.args[0] == "403 Forbidden"
    application.assert_not_called()


def hello(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [(environ["SCRIPT_NAME"] + environ["PATH_INFO"]).encode()]


def test_real_backend_loopback_authentication_shutdown():
    backend = launcher.Backend(hello, "/_desktop/test-token")
    opener = build_opener(ProxyHandler({}))
    try:
        backend.start()
        assert backend.server.socket.getsockname()[0] == "127.0.0.1"
        port = backend.server.socket.getsockname()[1]
        assert port > 0
        with opener.open(backend.url, timeout=3) as response:
            assert response.read() == b"/_desktop/test-token/"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["Cache-Control"] == "no-store"
            assert "form-action 'self'" in response.headers["Content-Security-Policy"]
            assert "Access-Control-Allow-Origin" not in response.headers
        for url, headers in ((backend.origin, {}), (backend.url, {"Origin": "http://127.0.0.1:2"})):
            with pytest.raises(HTTPError) as denied:
                opener.open(Request(url, headers=headers), timeout=3)
            assert denied.value.code == 403
            denied.value.close()
    finally:
        backend.close()
    assert not backend.thread.is_alive()
    assert not backend.server.task_dispatcher.threads
    assert not backend.server._map
    import socket

    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_readiness_failure_can_close_all_resources():
    backend = launcher.Backend(hello, "/_desktop/test-token")
    try:
        with pytest.raises(DesktopError):
            backend.start(timeout=0)
    finally:
        backend.close()
    assert not backend.server._map
    assert not backend.server.task_dispatcher.threads


@pytest.mark.parametrize("failure", [None, "startup", "gui"])
def test_native_lifecycle_has_no_browser_and_always_closes(profile, monkeypatch, failure):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", Mock(side_effect=AssertionError("Browser launch")))
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher, "default_profile", lambda: profile)
    monkeypatch.setattr(launcher, "configure_environment", Mock())
    monkeypatch.setattr(launcher, "create_application", Mock(return_value=hello))
    monkeypatch.setattr(launcher.logging, "disable", Mock())
    backend = Mock(url="http://127.0.0.1:1234/_desktop/token/")
    if failure == "startup":
        backend.start.side_effect = RuntimeError("private details")
    monkeypatch.setattr(launcher, "Backend", Mock(return_value=backend))
    class ClosingEvent(list):
        def __iadd__(self, callback):
            self.append(callback)
            return self

    closing_event = ClosingEvent()
    window = SimpleNamespace(events=SimpleNamespace(closing=closing_event))
    webview = SimpleNamespace(settings={}, create_window=Mock(return_value=window), start=Mock())
    if failure == "gui":
        webview.start.side_effect = RuntimeError("private details")
    elif failure is None:
        webview.start.side_effect = lambda **kwargs: closing_event[0]()
    monkeypatch.setitem(sys.modules, "webview", webview)
    if failure:
        with pytest.raises(RuntimeError):
            launcher.run()
    else:
        launcher.run()
        webview.create_window.assert_called_once()
        webview.start.assert_called_once_with(gui="cocoa", debug=False, private_mode=True, http_server=False)
    backend.close.assert_called_once()
    if failure == "startup":
        webview.create_window.assert_not_called()
    assert webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is False
    assert webview.settings["ALLOW_FILE_URLS"] is False
    webbrowser.open.assert_not_called()
    with ProfileLock(profile):
        pass


def test_startup_error_hides_exception_and_configuration(monkeypatch):
    monkeypatch.setattr(launcher, "run", Mock(side_effect=RuntimeError("password/private/path")))
    alert = Mock()
    monkeypatch.setattr(launcher, "show_error", alert)
    assert launcher.main() == 1
    message = alert.call_args.args[0]
    assert "password" not in message
    assert "/private/path" not in message
    assert "desktop-macos.md" in message


def test_test_delivery_blocks_even_injected_adapters(settings, monkeypatch):
    from delivery import credentials, transport

    settings.MARJAN_DESKTOP_TEST = True
    get_password = Mock(side_effect=AssertionError("Keychain called"))
    smtp = Mock(side_effect=AssertionError("SMTP called"))
    monkeypatch.setattr(credentials.keyring, "get_password", get_password)
    with pytest.raises(credentials.CredentialError, match="disabled"):
        credentials.load_password(None)
    assert not transport.test_smtp_connection(None, None, password_loader=get_password, smtp_factory=smtp).success
    assert not transport.send_prepared_email(None, None, None, on_submission_started=Mock(), password_loader=get_password, smtp_factory=smtp).success
    with pytest.raises(credentials.CredentialError, match="disabled"):
        transport._open(None, "", smtp)
    get_password.assert_not_called()
    smtp.assert_not_called()


def test_archive_rejects_cloud_paths_before_probe(profile, settings, monkeypatch):
    from archive import configuration
    from archive.services import ArchiveRoots

    settings.MARJAN_DESKTOP_TEST = True
    settings.MARJAN_TEST_ARCHIVE_PATHS = profile.archive_paths
    settings.MEDIA_ROOT = profile.root / "media"
    probe = Mock(side_effect=AssertionError("Probe called"))
    monkeypatch.setattr(configuration, "_verify_root", probe)
    for roots in ((profile.root / "real-cloud", *profile.archive_paths[1:]), ("relative", "other", "third")):
        with pytest.raises(ValueError):
            configuration.validate_archive_locations(*roots)
        with pytest.raises(ValueError):
            ArchiveRoots(*roots[:2])
    assert configuration.validate_archive_locations(*profile.archive_paths).local_root == profile.archive_paths[0]
    probe.assert_not_called()


@pytest.mark.parametrize("mode,owner,valid", [
    (0o600, 100, True), (0o644, 100, False), (0o600, 200, False),
])
def test_posix_private_file_policy_on_mocked_os(profile, monkeypatch, mode, owner, valid):
    import desktop.profile as profiles

    monkeypatch.setattr(profiles, "os", SimpleNamespace(name="posix", getuid=lambda: 100))
    path = SimpleNamespace(
        is_absolute=lambda: True,
        lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner, st_nlink=1),
    )
    path.resolve = lambda: path
    if valid:
        profile.private(path)
    else:
        with pytest.raises(DesktopError):
            profile.private(path)


def test_frozen_native_library_resolution_uses_bundle(monkeypatch, tmp_path):
    opened = []

    class FakeFFI:
        def dlopen(self, name, flags=0):
            opened.append((name, flags))

    monkeypatch.setitem(sys.modules, "cffi", SimpleNamespace(FFI=FakeFFI))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    runpy.run_path(str(REPOSITORY / "packaging/macos/runtime.py"))
    for name, expected in (
        ("libgobject-2.0-0", "libgobject-2.0.0.dylib"),
        ("libpango-1.0-0", "libpango-1.0.0.dylib"),
        ("libharfbuzz-0", "libharfbuzz.0.dylib"),
        ("libfontconfig-1", "libfontconfig.1.dylib"),
        ("libpangoft2-1.0-0", "libpangoft2-1.0.0.dylib"),
    ):
        FakeFFI().dlopen(name, 2)
        assert opened[-1] == (str(tmp_path / expected), 2)
    FakeFFI().dlopen("unrelated", 1)
    assert opened[-1] == ("unrelated", 1)


def test_explicit_setup_and_real_desktop_configuration(tmp_path):
    """Fresh process: real migrations only in provisioning, real WSGI, no GUI."""
    script = r'''
import hashlib, os, sqlite3, sys
from pathlib import Path
import desktop.profile as profiles
from desktop.setup_test import provision
from desktop.launcher import Backend, create_application, validate_database
from django.core.management import call_command
profile = profiles.TestProfile(Path(sys.argv[1]))
profiles.default_profile = lambda: profile
provision(profile)
original = hashlib.sha256(profile.database.read_bytes()).hexdigest()
os.environ['DJANGO_DEBUG'] = '1'
os.environ['MARJAN_DB_PATH'] = str(profile.root / 'production.sqlite3')
os.environ['MARJAN_SMTP_HOST'] = 'real.invalid'
profiles.configure_environment(profile)
import django
django.setup()
from django.conf import settings
from django.test import Client
assert settings.DEBUG is False
assert settings.ALLOWED_HOSTS == ['127.0.0.1']
assert settings.DATABASES['default']['NAME'] == profile.database
assert settings.MARJAN_SMTP_HOST == settings.MARJAN_KEYRING_SERVICE == ''
assert settings.MEDIA_ROOT == profile.root / 'media'
assert settings.RENDERING_ROOT == profile.root / 'rendered'
assert settings.SECRET_KEY == profile.load()
def forbidden(*args, **kwargs):
    raise AssertionError('Implicit provisioning/migration')
import app.provision_database
app.provision_database.main = forbidden
import django.core.management
django.core.management.call_command = forbidden
from django.db.migrations.executor import MigrationExecutor
MigrationExecutor.migrate = forbidden
prefix = '/_desktop/integration-token'
application = create_application(profile, prefix)
call_command('check', databases=['default'], verbosity=0)
backend = Backend(application, prefix)
try:
    backend.start()
    assert hashlib.sha256(profile.database.read_bytes()).hexdigest() == original
    from urllib.request import build_opener, ProxyHandler, Request
    from urllib.error import HTTPError
    opener = build_opener(ProxyHandler({}))
    with opener.open(backend.url, timeout=10) as response:
        html = response.read().decode()
        assert response.status == 200
        assert '/_desktop/integration-token/static/ui/app.css' in html, html
        assert 'fonts.googleapis.com' not in html
        assert 'csrfmiddlewaretoken' in html
        assert '/_desktop/integration-token/zacetna-nastavitev/' in response.url
    with opener.open(backend.url + 'static/ui/app.css', timeout=10) as response:
        assert response.status == 200
        assert b'--background:' in response.read()
        assert 'Access-Control-Allow-Origin' not in response.headers
    try:
        opener.open(Request(backend.url + 'nastavitve/arhiv/', data=b'x=1'), timeout=10)
    except HTTPError as error:
        assert error.code == 403
        assert b'integration-token' not in error.read()
        error.close()
    else:
        raise AssertionError('CSRF was bypassed')
finally:
    backend.close()
try:
    provision(profile)
except FileExistsError:
    pass
else:
    raise AssertionError('Existing TEST installation was reused')
with sqlite3.connect(profile.database.as_uri() + '?mode=rw', uri=True) as db:
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('django_migrations','django_content_type','sqlite_sequence')").fetchall()
    for (table,) in tables:
        expected = 1 if table == 'ui_setupnavigation' else 0
        assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (expected,), table
    db.execute("DELETE FROM django_migrations WHERE rowid=(SELECT min(rowid) FROM django_migrations)")
try:
    validate_database(profile)
except profiles.DesktopError:
    pass
else:
    raise AssertionError('Incompatible schema accepted')
print('Desktop checks passed; database unchanged; no implicit migration; business tables empty.')
'''
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("MARJAN_", "DJANGO_"))}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "private-TEST")], cwd=REPOSITORY,
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Desktop checks passed" in result.stdout
