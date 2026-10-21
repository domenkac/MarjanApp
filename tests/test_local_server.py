"""Production browser startup uses only disposable installation data."""

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


def clean_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("MARJAN_", "DJANGO_"))}


def direct_python(arguments):
    """Kill the real test process, not Windows' venv redirector parent."""
    if os.name != "nt":
        return [sys.executable, *arguments]
    import sysconfig

    bootstrap = "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); "
    if arguments[0] == "-c":
        bootstrap += "exec(sys.argv.pop(1))"
        tail = arguments[1:]
    else:
        assert arguments[:2] == ["-m", "app.local_server"]
        bootstrap += "sys.argv[0]='app.local_server'; runpy.run_module('app.local_server',run_name='__main__')"
        tail = arguments[2:]
    return [sys._base_executable, "-u", "-c", bootstrap, sysconfig.get_paths()["purelib"], *tail]


@pytest.fixture(scope="module")
def empty_database(tmp_path_factory):
    root = tmp_path_factory.mktemp("production-source-db").resolve()
    root.chmod(0o700)
    target = root / "new.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "app.provision_database", "--confirm-new-install"],
        cwd=REPOSITORY, env={**clean_environment(), "MARJAN_DB_PATH": str(target)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return target.read_bytes()


@pytest.fixture
def installation(tmp_path, empty_database):
    tmp_path.chmod(0o700)
    for name in ("media", "rendered"):
        (tmp_path / name).mkdir(mode=0o700)
    database = tmp_path / "live.sqlite3"
    database.write_bytes(empty_database)
    database.chmod(0o600)
    secret = tmp_path / "django-secret"
    secret.write_text("synthetic-stable-secret-" + "x" * 64, encoding="ascii")
    secret.chmod(0o600)
    values = {
        "MARJAN_DB_PATH": str(database),
        "MARJAN_MEDIA_ROOT": str(tmp_path / "media"),
        "MARJAN_RENDERING_ROOT": str(tmp_path / "rendered"),
        "DJANGO_SECRET_KEY_FILE": str(secret),
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    path.chmod(0o600)
    return path, values


def invoke(path, *arguments, environment=None):
    return subprocess.run(
        [sys.executable, "-m", "app.local_server", "--config", str(path), *arguments],
        cwd=REPOSITORY, env=environment or clean_environment(),
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_existing_provisioned_database_can_be_checked_without_changes(installation):
    path, values = installation
    database = Path(values["MARJAN_DB_PATH"])
    original = database.read_bytes()
    result = invoke(path, "--check")
    assert result.returncode == 0, result.stderr
    assert "Production configuration and database verified." in result.stdout
    assert database.read_bytes() == original


def test_file_secret_is_not_exported_to_process_or_child_environment(installation, monkeypatch):
    from app.server_config import configure_environment, load_configuration

    path, _values = installation
    configuration = load_configuration(str(path))
    with monkeypatch.context() as environment:
        # Restore the parent test environment after exercising its real mutation.
        for name in tuple(os.environ):
            if name.startswith(("MARJAN_", "DJANGO_")):
                environment.delenv(name)
        environment.setenv("DJANGO_SECRET_KEY", "untrusted-inherited-secret")
        for name in ("MARJAN_SERVER_CONFIG", "DJANGO_SETTINGS_MODULE", "DJANGO_DEBUG",
                     "MARJAN_DB_PATH", "MARJAN_MEDIA_ROOT", "MARJAN_RENDERING_ROOT",
                     *configuration.smtp):
            environment.setenv(name, "synthetic-before-configure")
        configure_environment(configuration)
        assert "DJANGO_SECRET_KEY" not in os.environ
        assert configuration.secret not in os.environ.values()
        result = subprocess.run(
            [sys.executable, "-c", "import os; assert 'DJANGO_SECRET_KEY' not in os.environ"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", [
    "MARJAN_DB_PATH", "MARJAN_MEDIA_ROOT", "MARJAN_RENDERING_ROOT", "DJANGO_SECRET_KEY_FILE",
])
def test_missing_production_configuration_fails_closed(installation, missing):
    path, values = installation
    values.pop(missing)
    path.write_text(json.dumps(values), encoding="utf-8")
    result = invoke(path, "--check")
    assert result.returncode == 1
    assert result.stderr.strip() == "Production startup failed. Check the private configuration, database, lock and local port."


def test_launchagent_is_opt_in_and_has_no_credentials():
    import plistlib

    path = REPOSITORY / "packaging" / "macos" / "si.marjanapp.local.plist.example"
    assert path.is_file()
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    assert value["RunAtLoad"] is False
    assert value["KeepAlive"] is False
    assert set(value["EnvironmentVariables"]) == {"DYLD_FALLBACK_LIBRARY_PATH"}
    assert value["ProgramArguments"][1:3] == ["-m", "app.local_server"]


@pytest.mark.parametrize("extra", [
    None, "extra.py", "app/extra.py", "app/extra.pyc", "ignored-native.dylib", "unreviewed.sh",
])
def test_documented_source_guard_rejects_untracked_and_ignored_entries(tmp_path, extra):
    import shlex

    guide = (REPOSITORY / "docs" / "local-browser-macos.md").read_text(encoding="utf-8")
    command = next(line for line in guide.splitlines() if line.startswith("git ls-files "))
    arguments = shlex.split(command.split(" > ", 1)[0])
    # Exercise only Git in a disposable empty repository; never run the Mac shell example.
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("*.pyc\n*.dylib\n", encoding="utf-8")
    for name in ("nespremenljivi-asseti", "slike"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "synthetic.py").write_text("synthetic protected-path fixture", encoding="utf-8")
    (tmp_path / "db_before_manual_test.sqlite3").write_bytes(b"synthetic fixture, not business data")
    if extra:
        target = tmp_path / extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic unreviewed source")
    result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, check=True)
    assert bool(result.stdout) is (extra is not None)
    assert b"synthetic.py" not in result.stdout
    assert b"db_before_manual_test.sqlite3" not in result.stdout
    assert 'if [ -s "$APP_EVIDENCE/untracked-source-paths.zlist" ]; then' in guide
    assert command.endswith('"$APP_EVIDENCE/untracked-source-paths.zlist" || exit 1')


@pytest.mark.parametrize("damage", ["missing", "empty", "foreign", "migration", "column"])
def test_wrong_database_fails_without_replacement(installation, damage):
    import sqlite3

    path, values = installation
    database = Path(values["MARJAN_DB_PATH"])
    if damage == "missing":
        database.unlink()
    elif damage in {"empty", "foreign"}:
        database.write_bytes(b"" if damage == "empty" else b"foreign non-SQLite content")
    else:
        with closing(sqlite3.connect(database)) as connection:
            if damage == "migration":
                connection.execute("DELETE FROM django_migrations WHERE id=(SELECT min(id) FROM django_migrations)")
            else:
                connection.execute("ALTER TABLE ui_setupnavigation ADD COLUMN unexpected TEXT")
            connection.commit()
    original = database.read_bytes() if database.exists() else None
    result = invoke(path, "--check")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert (database.read_bytes() if database.exists() else None) == original


@pytest.mark.parametrize("field,value", [
    ("MARJAN_DB_PATH", ":memory:"), ("MARJAN_DB_PATH", "relative.sqlite3"),
    ("MARJAN_MEDIA_ROOT", "relative"), ("MARJAN_RENDERING_ROOT", ""),
    ("MARJAN_SMTP_PASSWORD", "synthetic-forbidden-password"),
])
def test_unsafe_configuration_is_rejected_without_disclosing_values(installation, field, value):
    path, values = installation
    values[field] = value
    path.write_text(json.dumps(values), encoding="utf-8")
    result = invoke(path, "--check")
    assert result.returncode == 1
    assert value not in result.stdout + result.stderr if len(value) > 10 else True
    assert "Traceback" not in result.stderr


def test_repository_and_test_profile_paths_are_rejected(installation, monkeypatch):
    from app.server_config import ServerError, load_configuration

    path, values = installation
    values["MARJAN_DB_PATH"] = str(REPOSITORY / "never-open.sqlite3")
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises((ServerError, OSError)):
        load_configuration(str(path))
    assert not (REPOSITORY / "never-open.sqlite3").exists()


@pytest.mark.parametrize("change", ["short-secret", "overlap", "unknown-debug", "missing-config"])
def test_private_configuration_limits(installation, change):
    path, values = installation
    if change == "short-secret":
        Path(values["DJANGO_SECRET_KEY_FILE"]).write_text("not-a-secret", encoding="ascii")
    elif change == "overlap":
        values["MARJAN_RENDERING_ROOT"] = values["MARJAN_MEDIA_ROOT"]
        path.write_text(json.dumps(values), encoding="utf-8")
    elif change == "unknown-debug":
        values["DJANGO_DEBUG"] = "1"
        path.write_text(json.dumps(values), encoding="utf-8")
    else:
        path.unlink()
    assert invoke(path, "--check").returncode == 1


@pytest.mark.parametrize("headers", [
    {"HTTP_HOST": "localhost:8000"}, {"HTTP_HOST": "evil.invalid:8000"},
    {"HTTP_HOST": "127.0.0.1:9000"}, {"REMOTE_ADDR": "192.0.2.1"},
    {"HTTP_ORIGIN": "https://evil.invalid"}, {"HTTP_ORIGIN": "null"},
    {"HTTP_SEC_FETCH_SITE": "cross-site"}, {"HTTP_SEC_FETCH_SITE": "same-site"},
])
def test_host_and_origin_gate_runs_before_application(headers):
    from app.local_gateway import LocalGateway

    environ = {"HTTP_HOST": "127.0.0.1:8000", "REMOTE_ADDR": "127.0.0.1", **headers}
    status = []
    gateway = LocalGateway(lambda *args: pytest.fail("Unsafe request reached Django"), 8000)
    assert gateway(environ, lambda value, headers: status.append(value)) == [b"Request denied."]
    assert status == ["403 Forbidden"]


def test_real_socket_binding_occupied_port_and_shutdown():
    import socket
    from urllib.request import ProxyHandler, build_opener

    from app.local_server import LocalServer

    def application(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"synthetic"]

    first = LocalServer(application, port=0)
    port = int(first.server.effective_port)
    assert first.server.socket.getsockname() == ("127.0.0.1", port)
    try:
        with pytest.raises(OSError):
            LocalServer(application, port=port)
        first.start()
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.read() == b"synthetic"
    finally:
        first.close()
    assert not first.thread.is_alive()
    assert not first.dispatcher.threads
    with closing(socket.socket()) as client:
        client.settimeout(1)
        assert client.connect_ex(("127.0.0.1", port)) != 0


def test_active_operation_finishes_before_shutdown_and_lock_release():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from urllib.request import ProxyHandler, build_opener

    from app.local_server import LocalServer

    started, release = Event(), Event()

    def application(environ, start_response):
        started.set()
        assert release.wait(10)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"completed"]

    server = LocalServer(application, port=0)
    server.start()
    url = f"http://127.0.0.1:{server.server.effective_port}/"
    def request():
        with build_opener(ProxyHandler({})).open(url, timeout=15) as response:
            return response.read()
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(request)
        try:
            assert started.wait(5)
            shutdown = workers.submit(server.close)
            assert not shutdown.done()
        finally:
            release.set()
        assert pending.result(timeout=10) == b"completed"
        shutdown.result(timeout=10)
    assert not server.thread.is_alive()


def test_kernel_lock_duplicate_crash_and_restart(installation):
    from app.local_server import InstanceLock

    _path, values = installation
    database = Path(values["MARJAN_DB_PATH"])
    script = "from app.local_server import InstanceLock; from pathlib import Path; import sys;\nwith InstanceLock(Path(sys.argv[1])): print('LOCKED', flush=True); sys.stdin.read()"
    with subprocess.Popen(
        direct_python(["-c", script, str(database)]), cwd=REPOSITORY,
        env=clean_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            assert process.stdout.readline().strip() == "LOCKED"
            with pytest.raises(OSError), InstanceLock(database):
                pass
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
    with InstanceLock(database) as lock:
        lock_path = lock.path
    assert lock_path.exists(), "Kernel lock file must never be unlinked"
    with InstanceLock(database):
        pass


def test_sanitized_logging_does_not_print_credentials(capsys):
    import logging

    from app.local_server import SafeFormatter

    record = logging.LogRecord("synthetic", logging.ERROR, "", 1, "private-password-value", (), None)
    assert "private-password-value" not in SafeFormatter().format(record)


def test_production_http_static_csrf_setup_and_no_startup_side_effects(installation):
    path, _values = installation
    script = r'''
import hashlib, importlib, os, re, sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from http.cookiejar import CookieJar
from app.server_config import load_configuration, configure_environment
from app.local_server import LocalServer, create_application
config = load_configuration(sys.argv[1])
configure_environment(config)
import django
django.setup()
from django.conf import settings
assert settings.DEBUG is False and settings.SECRET_KEY == config.secret
assert settings.ALLOWED_HOSTS == ['127.0.0.1']
assert not getattr(settings, 'MARJAN_DESKTOP_TEST', False)
assert settings.MARJAN_SMTP_HOST == ''
assert settings.DATABASES['default']['NAME'] == config.database
assert settings.EMAIL_BACKEND != 'django.core.mail.backends.dummy.EmailBackend'
from django.core.management import call_command
call_command('check', databases=['default'], verbosity=0)
module = importlib.import_module('app.production_settings')
assert importlib.reload(module).SECRET_KEY == config.secret
assert 'DJANGO_SECRET_KEY' not in os.environ
assert config.secret not in os.environ.values()
def forbidden(*args, **kwargs):
    raise AssertionError('Forbidden startup operation')
import app.provision_database, django.core.management, keyring, smtplib, webbrowser
from django.db.migrations.executor import MigrationExecutor
app.provision_database.main = forbidden
django.core.management.call_command = forbidden
MigrationExecutor.migrate = forbidden
keyring.get_password = keyring.set_password = forbidden
smtplib.SMTP = smtplib.SMTP_SSL = forbidden
webbrowser.open = forbidden
from archive import configuration as archive_configuration
archive_configuration.validate_archive_locations = forbidden
original = hashlib.sha256(config.database.read_bytes()).hexdigest()
application = create_application(config)
assert hashlib.sha256(config.database.read_bytes()).hexdigest() == original
server = LocalServer(application, port=0)
server.start()
url = 'http://127.0.0.1:' + str(server.server.effective_port)
jar = CookieJar()
client = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))
try:
    with client.open(url + '/', timeout=10) as response:
        html = response.read().decode()
        assert response.status == 200 and '/zacetna-nastavitev/' in response.url
        assert 'fonts.googleapis.com' not in html and 'fonts.gstatic.com' not in html
    with client.open(url + '/static/ui/app.css', timeout=10) as response:
        assert b'--background:' in response.read()
        assert response.headers.get('Access-Control-Allow-Origin') is None
    target = url + '/zacetna-nastavitev/?korak=unit'
    # A valid setup navigation POST is accepted without approving any business readiness.
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)[1]
    body = urlencode({'action': 'restart', 'csrfmiddlewaretoken': token}).encode()
    with client.open(Request(target, data=body, headers={'Origin': url}), timeout=10) as response:
        assert response.status == 200
    for request in [
        Request(target, data=b'action=restart'),
        Request(target, data=body, headers={'Origin': 'http://evil.invalid'}),
        Request(url + '/', headers={'Host': 'evil.invalid'}),
    ]:
        try:
            client.open(request, timeout=10)
        except HTTPError as error:
            assert error.code == 403
            assert b'Traceback' not in error.read()
            error.close()
        else:
            raise AssertionError('Unsafe request accepted')
    from companies.models import Company, NumberSequence, InitialSetupCompletion
    from invoicing.models import Invoice
    assert not Company.objects.exists() and not NumberSequence.objects.exists()
    assert not Invoice.objects.exists() and not InitialSetupCompletion.objects.exists()
finally:
    server.close()
print('HTTP/static/CSRF/setup verified; no startup migrations, provisioning, SMTP, Keychain, cloud or browser call.')
'''
    environment = {**clean_environment(), "MARJAN_DB_PATH": str(path.parent / "never-created.sqlite3"),
                   "DJANGO_SETTINGS_MODULE": "desktop.settings", "DJANGO_DEBUG": "1",
                   "DJANGO_SECRET_KEY": "untrusted-inherited-secret"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], cwd=REPOSITORY, env=environment,
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HTTP/static/CSRF/setup verified" in result.stdout
    assert not (path.parent / "never-created.sqlite3").exists()


def test_configuration_change_during_settings_import_fails_closed(installation):
    path, values = installation
    script = r'''
import json, sys
from pathlib import Path
from app import local_server
original = local_server.load_configuration
def change(path):
    configuration = original(path)
    values = json.loads(Path(path).read_text())
    alternate = Path(path).parent / 'alternate-rendered'
    alternate.mkdir(mode=0o700)
    values['MARJAN_RENDERING_ROOT'] = str(alternate)
    Path(path).write_text(json.dumps(values))
    return configuration
local_server.load_configuration = change
raise SystemExit(local_server.main(['--config', sys.argv[1], '--check']))
'''
    original_bytes = Path(values["MARJAN_DB_PATH"]).read_bytes()
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=REPOSITORY,
                            env=clean_environment(), capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert Path(values["MARJAN_DB_PATH"]).read_bytes() == original_bytes


def test_root_user_is_rejected_before_loading_any_data(monkeypatch):
    from app import local_server
    from app.server_config import ServerError

    monkeypatch.setattr(local_server.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(local_server, "load_configuration", lambda _: pytest.fail("Root opened data"))
    with pytest.raises(ServerError, match="root"):
        local_server.run("never-open")


def test_unc_path_is_rejected_before_network_resolution():
    from app.server_config import ServerError, private_path

    with pytest.raises(ServerError):
        private_path(r"\\forbidden.invalid\share\production.sqlite3")


def test_cli_fixed_url_duplicate_instance_and_crash_restart(installation):
    import queue
    import socket
    import threading

    path, _values = installation
    with closing(socket.socket()) as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError:
            pytest.skip("Port 8000 already occupied; do not contact or stop its owner")
    processes = []
    try:
        for _ in range(2):
            process = subprocess.Popen(
                direct_python(["-m", "app.local_server", "--config", str(path)]),
                cwd=REPOSITORY, env=clean_environment(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            lines = queue.Queue()
            threading.Thread(target=lambda lines=lines, process=process: lines.put(process.stdout.readline()), daemon=True).start()
            assert lines.get(timeout=30).strip() == "MarjanApp listening at http://127.0.0.1:8000/"
            duplicate = invoke(path)
            assert duplicate.returncode == 1
            assert "Traceback" not in duplicate.stderr
            process.kill()
            process.wait(timeout=10)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("option", ["--host", "--port"])
def test_cli_has_no_external_binding_override(installation, option):
    path, _values = installation
    result = invoke(path, option, "0.0.0.0" if option == "--host" else "9000")
    assert result.returncode == 2


def test_lock_symlink_or_hardlink_is_rejected_without_mutation(installation):
    from app.local_server import InstanceLock
    from app.server_config import ServerError

    _path, values = installation
    database = Path(values["MARJAN_DB_PATH"])
    target = database.parent / "unrelated-private-file"
    target.write_bytes(b"untouched")
    target.chmod(0o600)
    lock = InstanceLock(database)
    os.link(target, lock.path)
    with pytest.raises(ServerError), lock:
        pytest.fail("Aliased lock accepted")
    assert target.read_bytes() == b"untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal termination requires native Mac acceptance")
def test_cli_sigterm_is_graceful(installation):
    import queue
    import signal
    import socket
    import threading

    path, _values = installation
    with closing(socket.socket()) as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError:
            pytest.skip("Port 8000 is occupied")
    with subprocess.Popen(
        [sys.executable, "-u", "-m", "app.local_server", "--config", str(path)],
        cwd=REPOSITORY, env=clean_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            assert "MarjanApp listening" in lines.get(timeout=30)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=30)
            assert process.returncode == 0 and "MarjanApp stopped." in output
            assert "Traceback" not in errors
        finally:
            if process.poll() is None:
                process.kill()
