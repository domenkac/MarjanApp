"""Explicit first installation: python -m app.provision_database --help.

Migrate a private in-memory database, then publish its verified bytes with an
exclusive hard link. SQLite never opens an operator-supplied file during setup.
Normal application settings and their mode=rw connection factory are unchanged.
"""

import argparse
import os
import sqlite3
import stat
import sys
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
SUCCESS = "New installation database provisioned and verified:"


def _target_path():
    value = os.environ.get("MARJAN_DB_PATH", "")
    if not value.strip() or "\0" in value:
        raise ValueError("MARJAN_DB_PATH must explicitly name an absolute new file.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("MARJAN_DB_PATH must be an absolute filesystem path.")
    # Path() normalizes dot components and repeated separators; inspect the input.
    spelling = os.fspath(path.anchor)
    expanded = os.path.expanduser(value)
    tail = expanded[len(spelling):]
    if os.name == "nt":
        if path.drive.startswith("\\\\"):
            raise ValueError("MARJAN_DB_PATH must use a local drive, not a UNC/device path.")
        tail = tail.replace("\\", "/")
        for part in tail.split("/"):
            stem = part.split(".")[0].upper()
            if (":" in part or part.endswith((" ", "."))
                    or stem in {"CON", "PRN", "AUX", "NUL"}
                    or stem in {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(10)}):
                raise ValueError("MARJAN_DB_PATH contains an unsafe Windows path alias.")
    if any(part in {"", ".", ".."} for part in tail.split("/")):
        raise ValueError("MARJAN_DB_PATH must not contain path aliases or dot components.")
    if path != path.resolve():
        raise ValueError("MARJAN_DB_PATH must not contain symlinks or path aliases.")
    if path.is_relative_to(REPOSITORY):
        raise ValueError("MARJAN_DB_PATH must be outside the repository.")
    return path


def _windows_pin(path, stack):
    """Reject reparse points and deny directory rename/delete until publication."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    info.restype = wintypes.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    for directory in [*reversed(path.parents), path]:
        # FILE_READ_ATTRIBUTES; share read/write but NOT delete; OPEN_EXISTING;
        # BACKUP_SEMANTICS | OPEN_REPARSE_POINT (inspect links, never follow them).
        handle = create(str(directory), 0x80, 0x3, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        stack.callback(close, handle)
        attributes = AttributeTag()
        if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & 0x400 or not attributes.attributes & 0x10:
            raise ValueError("MARJAN_DB_PATH parent is a reparse point or not a directory.")


@contextmanager
def _pinned_parent(path):
    """Pin each ancestor before descending, so later renames cannot redirect IO."""
    with ExitStack() as stack:
        if os.name == "nt":
            _windows_pin(path, stack)
            descriptor = None
        elif os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(path.anchor, flags)
            stack.callback(os.close, descriptor)
            for component in path.parts[1:]:
                descriptor = os.open(component, flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
        else:
            raise ValueError("Safe provisioning is unsupported on this platform.")
        parent = path, descriptor
        _verify_parent(parent)
        yield parent
        _verify_parent(parent)


def _verify_parent(parent):
    path, descriptor = parent
    if path != path.resolve(strict=True):
        raise ValueError("MARJAN_DB_PATH parent changed or contains an alias.")
    if descriptor is not None:
        pinned = os.fstat(descriptor)
        if not os.path.samestat(pinned, path.stat()):
            raise ValueError("MARJAN_DB_PATH parent was replaced or renamed.")
        if pinned.st_uid != os.getuid() or pinned.st_mode & 0o022:
            raise ValueError("Database parent must be owned by this user and not writable by group/others.")


def _entry(parent, name):
    path, descriptor = parent
    return name if descriptor is not None else path / name


def _assert_absent(parent, name):
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.stat(_entry(parent, name + suffix), dir_fd=parent[1], follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError(f"Target or SQLite sidecar already exists: {name + suffix}")


def _verify_database(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity_check failed.")
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise ValueError("SQLite foreign key enforcement is unavailable.")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SQLite foreign_key_check failed.")


def _new_database():
    import django
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    if settings.configured:
        raise ValueError("Run provisioning in a fresh standalone Python process.")
    if os.environ.get("DJANGO_SETTINGS_MODULE", "app.settings") != "app.settings":
        raise ValueError("Provisioning requires the unmodified app.settings module.")
    os.environ["DJANGO_SETTINGS_MODULE"] = "app.settings"
    # This override exists only in this standalone process. No file connection or
    # test database can be opened, even if a migration closes/reopens a connection.
    settings.DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
        "OPTIONS": {"transaction_mode": "IMMEDIATE"},
    }}
    django.setup()
    try:
        call_command("migrate", interactive=False, verbosity=0)
        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise ValueError("Pending migrations remain after provisioning.")
        if set(executor.loader.applied_migrations) != set(executor.loader.graph.nodes):
            raise ValueError("Applied migrations do not match the migration graph.")
        call_command("check", databases=["default"], fail_level="WARNING", verbosity=0)
        _verify_database(connection.connection)
        data = connection.connection.serialize()
        return data, len(executor.loader.applied_migrations)
    finally:
        connection.close()


def _publish(parent, name, data):
    """Only write through our exclusive descriptor; hard-link publication cannot replace."""
    _assert_absent(parent, name)
    temporary = f".marjan-provision-{uuid.uuid4().hex}.sqlite3"
    descriptor = os.open(
        _entry(parent, temporary), os.O_CREAT | os.O_EXCL | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        0o600, dir_fd=parent[1],
    )
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            persisted = stream.read()
            if persisted != data:
                raise ValueError("Persisted SQLite bytes differ from the verified database.")
            with closing(sqlite3.connect(":memory:")) as verification:
                verification.deserialize(persisted)
                _verify_database(verification)
            current = os.stat(_entry(parent, temporary), dir_fd=parent[1], follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or not os.path.samestat(current, os.fstat(stream.fileno())):
                raise ValueError("Private provisioning file changed before publication.")
            _assert_absent(parent, name)
            os.link(
                _entry(parent, temporary), _entry(parent, name),
                src_dir_fd=parent[1], dst_dir_fd=parent[1], follow_symlinks=False,
            )
    finally:
        # Never delete the target, including after a failed final durability check.
        os.unlink(_entry(parent, temporary), dir_fd=parent[1])
    if parent[1] is not None:
        os.fsync(parent[1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-paths", action="store_true", help="Validate and display paths without writing anything.")
    action.add_argument("--confirm-new-install", action="store_true", help="Explicitly authorize a brand-new, empty installation.")
    args = parser.parse_args(argv)
    try:
        target = _target_path()
        with _pinned_parent(target.parent) as parent:
            _assert_absent(parent, target.name)
            print(f"MARJAN_DB_PATH={target}")
            # Read settings only for the same expanded paths normal startup uses.
            from app import settings as application_settings

            print(f"MARJAN_MEDIA_ROOT={application_settings.MEDIA_ROOT}")
            print(f"MARJAN_RENDERING_ROOT={application_settings.RENDERING_ROOT}")
            if args.check_paths:
                print("Paths checked. No files or directories created.")
                return 0
            data, migrations = _new_database()
            _verify_parent(parent)
            _publish(parent, target.name, data)
        print(f"{SUCCESS} {target} ({migrations} migrations).")
        print("Initial setup remains incomplete. Complete the first-run wizard before issuance.")
        return 0
    except Exception as error:  # noqa: BLE001 -- CLI boundary: all failures need a diagnostic and nonzero status.
        print(
            f"Provisioning failed: {error}\n"
            "Do not start the application. Inspect the configured paths and permissions. "
            "Existing targets are never repaired or reused. If a target exists, retain it "
            "for diagnosis and choose a different unused path for a new installation. "
            "No fallback database was opened.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
