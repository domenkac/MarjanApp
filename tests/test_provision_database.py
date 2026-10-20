"""The installer runs in a separate process and never opens a repository DB."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from django.db.migrations.loader import MigrationLoader

REPOSITORY = Path(__file__).resolve().parents[1]
SUCCESS = "New installation database provisioned and verified:"


def run_installer(tmp_path, value, *arguments, fault=""):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("MARJAN_DB_PATH", "DJANGO_SETTINGS_MODULE"):
        env.pop(name, None)
    if value is not None:
        env["MARJAN_DB_PATH"] = str(value)
    env["MARJAN_MEDIA_ROOT"] = str(tmp_path / "unused-media")
    env["MARJAN_RENDERING_ROOT"] = str(tmp_path / "unused-rendering")
    script = """
import sys
def guard(event, args):
    if event == 'sqlite3.connect' and args[0] != ':memory:':
        raise RuntimeError('Installer tried opening a file database')
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('Installer tried network access')
sys.addaudithook(guard)
""" + fault + """
from app.provision_database import main
sys.exit(main(sys.argv[1:]))
"""
    return subprocess.run(
        [sys.executable, "-B", "-c", script, *arguments],
        cwd=REPOSITORY, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_new_install_migrations_readiness_and_issuance(tmp_path):
    database = tmp_path.resolve() / "Marjan č #%.sqlite3"
    result = run_installer(tmp_path, database, "--confirm-new-install")
    assert result.returncode == 0, result.stderr
    assert SUCCESS in result.stdout
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        migrations = db.execute("SELECT app, name FROM django_migrations").fetchall()
        # Load every installed app's graph, including Django's contenttypes.
        expected_migrations = set(MigrationLoader(None).graph.nodes)
        assert set(migrations) == expected_migrations
        assert len(migrations) == len(expected_migrations)
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN "
            "('django_migrations', 'django_content_type', 'sqlite_sequence')"
        ).fetchall()
        for (table,) in tables:
            assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
    assert list(tmp_path.iterdir()) == [database]
    # Restart with ordinary settings: exercise the real setup and issuance gates.
    script = """
import django, json, sys
from pathlib import Path
target = Path(sys.argv[1])
def guard(event, args):
    if event == 'sqlite3.connect':
        if args[0] != target.as_uri() + '?mode=rw':
            raise RuntimeError('Unexpected database')
sys.addaudithook(guard)
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.test.utils import setup_test_environment
from django.core.exceptions import ValidationError
from django.utils import timezone
from companies.readiness import initial_setup_completed, complete_initial_setup
from companies.services import dodeli_naslednjo_stevilko
executor = MigrationExecutor(connection)
executor.loader.check_consistent_history(connection)
assert not executor.migration_plan(executor.loader.graph.leaf_nodes())
setup_test_environment()
client = Client(HTTP_HOST='127.0.0.1')
response = client.get('/', follow=True)
assert response.status_code == 200
assert response.redirect_chain == [('/zacetna-nastavitev/', 302)]
assert response.context['checked_count'] == 0
assert not initial_setup_completed()
for operation in [complete_initial_setup] + [
    lambda tip=tip: dodeli_naslednjo_stevilko(None, tip, timezone.localdate())
    for tip in ('NP', 'ST')
]:
    try:
        operation()
    except ValidationError:
        pass
    else:
        raise AssertionError('Setup or issuance unexpectedly allowed')
print(json.dumps({'wizard': 'incomplete', 'issuance': 'blocked'}))
"""
    restarted = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database)], cwd=REPOSITORY,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "app.settings",
             "MARJAN_DB_PATH": str(database),
             "MARJAN_MEDIA_ROOT": str(tmp_path / "unused-media"),
             "MARJAN_RENDERING_ROOT": str(tmp_path / "unused-rendering")},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout) == {"wizard": "incomplete", "issuance": "blocked"}


@pytest.mark.parametrize("contents", [b"", b"partial database", b"SQLite format 3\0"])
def test_existing_file_never_changes(tmp_path, contents):
    database = tmp_path.resolve() / "existing.sqlite3"
    database.write_bytes(contents)
    before = database.stat()
    result = run_installer(tmp_path, database, "--confirm-new-install")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert database.read_bytes() == contents
    after = database.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns,
    )
    assert SUCCESS not in result.stdout


def test_repeated_invocation_fails_without_changing_database(tmp_path):
    database = tmp_path.resolve() / "new.sqlite3"
    assert run_installer(tmp_path, database, "--confirm-new-install").returncode == 0
    before = database.read_bytes()
    result = run_installer(tmp_path, database, "--confirm-new-install")
    assert result.returncode != 0
    assert database.read_bytes() == before
    assert SUCCESS not in result.stdout


@pytest.mark.parametrize("value", [None, "", "relative.sqlite3", ":memory:", "file:new"])
def test_requires_explicit_absolute_path(tmp_path, value):
    result = run_installer(tmp_path, value, "--confirm-new-install")
    assert result.returncode != 0
    assert "MARJAN_DB_PATH" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_confirmation_required_and_path_preview_is_read_only(tmp_path):
    database = tmp_path.resolve() / "new.sqlite3"
    result = run_installer(tmp_path, database)
    assert result.returncode != 0
    assert "--confirm-new-install" in result.stderr
    preview = run_installer(tmp_path, database, "--check-paths")
    assert preview.returncode == 0, preview.stderr
    assert str(database) in preview.stdout
    assert "MARJAN_MEDIA_ROOT" in preview.stdout
    assert "MARJAN_RENDERING_ROOT" in preview.stdout
    assert SUCCESS not in preview.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["repository", "parent-missing", "dotdot", "directory", "sidecar"])
def test_unsafe_paths_rejected(tmp_path, kind):
    database = tmp_path.resolve() / "new.sqlite3"
    if kind == "repository":
        database = REPOSITORY / "must-not-be-created.sqlite3"
    elif kind == "parent-missing":
        database = tmp_path.resolve() / "absent" / "new.sqlite3"
    elif kind == "dotdot":
        database = str(tmp_path.resolve()) + "/unused/../new.sqlite3"
    elif kind == "directory":
        database.mkdir()
    else:
        database.with_name(database.name + "-wal").write_bytes(b"keep")
    result = run_installer(tmp_path, database, "--confirm-new-install")
    assert result.returncode != 0
    assert SUCCESS not in result.stdout
    if kind != "directory":
        assert not Path(database).exists()


@pytest.mark.parametrize("kind", ["target", "parent", "dangling"])
def test_symlinks_rejected(tmp_path, kind):
    actual = tmp_path.resolve() / "actual"
    actual.mkdir()
    link = tmp_path.resolve() / "alias"
    source = actual if kind == "parent" else actual / "existing.sqlite3"
    if kind == "target":
        source.write_bytes(b"keep")
    try:
        link.symlink_to(source, target_is_directory=kind == "parent")
    except OSError as error:
        pytest.skip(f"Symlinks unavailable on this host: {error}")
    target = link / "new.sqlite3" if kind == "parent" else link
    result = run_installer(tmp_path, target, "--confirm-new-install")
    assert result.returncode != 0
    assert SUCCESS not in result.stdout
    if kind == "target":
        assert source.read_bytes() == b"keep"


@pytest.mark.parametrize("stage", ["migrate", "check", "verify", "publish", "fsync", "link", "persisted"])
def test_failed_initialization_never_reports_success(tmp_path, stage):
    database = tmp_path.resolve() / "new.sqlite3"
    if stage in {"migrate", "check"}:
        fault = f"""
from django.core import management
original = management.call_command
def fail(name, *args, **kwargs):
    if name == {stage!r}:
        raise RuntimeError('Injected {stage} failure')
    return original(name, *args, **kwargs)
management.call_command = fail
"""
    elif stage in {"verify", "publish"}:
        fault = f"""
import app.provision_database as installer
def fail(*args, **kwargs):
    raise RuntimeError('Injected {stage} failure')
installer.{'_verify_database' if stage == 'verify' else '_publish'} = fail
"""
    elif stage == "persisted":
        fault = """
import app.provision_database as installer
installer._new_database = lambda: (b'not a SQLite database', 50)
"""
    else:
        fault = f"""
import app.provision_database as installer
def fail(*args, **kwargs):
    raise OSError('Injected {stage} failure')
installer.os.{stage} = fail
"""
    result = run_installer(tmp_path, database, "--confirm-new-install", fault=fault)
    assert result.returncode != 0
    assert "failed" in result.stderr.lower()
    assert SUCCESS not in result.stdout
    assert not database.exists()
    assert list(tmp_path.iterdir()) == []


def test_competing_creator_cannot_be_overwritten(tmp_path):
    database = tmp_path.resolve() / "new.sqlite3"
    fault = """
import app.provision_database as installer
original = installer.os.link
def competing(source, destination, **kwargs):
    import os
    with open(os.environ['MARJAN_DB_PATH'], 'xb') as stream:
        stream.write(b'competing creator')
    return original(source, destination, **kwargs)
installer.os.link = competing
"""
    result = run_installer(tmp_path, database, "--confirm-new-install", fault=fault)
    assert result.returncode != 0
    assert database.read_bytes() == b"competing creator"
    assert list(tmp_path.iterdir()) == [database]
    assert SUCCESS not in result.stdout


def test_verification_rejects_foreign_key_violation():
    from app.provision_database import _verify_database

    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
            "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));"
            "INSERT INTO child VALUES(42);"
        )
        with pytest.raises(ValueError, match="foreign_key_check failed"):
            _verify_database(db)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction protection")
def test_windows_junction_parent_rejected(tmp_path):
    import _winapi

    actual = tmp_path.resolve() / "actual"
    actual.mkdir()
    alias = tmp_path.resolve() / "alias"
    _winapi.CreateJunction(str(actual), str(alias))
    result = run_installer(tmp_path, alias / "new.sqlite3", "--confirm-new-install")
    assert result.returncode != 0
    assert not (actual / "new.sqlite3").exists()
    assert SUCCESS not in result.stdout


def test_normal_startup_never_initializes_missing_database(tmp_path):
    database = tmp_path.resolve() / "missing.sqlite3"
    script = """
import sys
from pathlib import Path
target = Path(sys.argv[1])
def guard(event, args):
    if event == 'sqlite3.connect' and args[0] != target.as_uri() + '?mode=rw':
        raise RuntimeError('Unexpected database connection')
sys.addaudithook(guard)
from django.core.wsgi import get_wsgi_application
get_wsgi_application()
from django.test import Client
from django.db import OperationalError
try:
    Client(HTTP_HOST='127.0.0.1').get('/')
except OperationalError:
    pass
else:
    raise AssertionError('Missing database startup did not fail closed')
assert not target.exists()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database)], cwd=REPOSITORY,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "app.settings",
             "MARJAN_DB_PATH": str(database),
             "MARJAN_MEDIA_ROOT": str(tmp_path / "unused-media"),
             "MARJAN_RENDERING_ROOT": str(tmp_path / "unused-rendering")},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
