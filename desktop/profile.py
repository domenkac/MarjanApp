"""Private installation identity; ordinary startup never creates installation data."""

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


class DesktopError(Exception):
    """Only these fixed messages may reach the native startup dialog."""


MISSING = (
    "TEST namestitev ni pripravljena. Izvajalec mora po navodilih "
    "docs/desktop-macos.md izrecno pripraviti novo TEST bazo. "
    "Obstoječe ali obnovljene baze ne priključujte."
)
INVALID = "TEST namestitev ni varna ali združljiva. Ustavite zagon in preverite navodila za namestitev."


@dataclass(frozen=True)
class TestProfile:
    __test__ = False
    root: Path

    @property
    def database(self):
        return self.root / "test.sqlite3"

    @property
    def archive_paths(self):
        return tuple(self.root / name for name in ("archive-local", "archive-icloud-test", "backup-dropbox-test"))

    @property
    def directories(self):
        return (self.root, self.root / "media", self.root / "rendered", *self.archive_paths)

    def private(self, path, *, directory=False):
        """Reject aliases, linked files, foreign ownership and POSIX public access."""
        if not path.is_absolute() or path.resolve() != path:
            raise DesktopError(INVALID)
        info = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
            raise DesktopError(INVALID)
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise DesktopError(INVALID)
        return info

    def load(self):
        try:
            for path in self.directories:
                self.private(path, directory=True)
            self.private(self.root / "profile.json")
            self.private(self.root / "secret.key")
            receipt = json.loads((self.root / "profile.json").read_text(encoding="utf-8"))
            info = self.private(self.database)
            if receipt != {"profile": "TEST", "version": 1, "database_id": [info.st_dev, info.st_ino]}:
                raise DesktopError(INVALID)
            secret = (self.root / "secret.key").read_text(encoding="ascii")
            if len(secret) != 86 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in secret):
                raise DesktopError(INVALID)
            return secret
        except FileNotFoundError:
            raise DesktopError(MISSING) from None
        except (OSError, ValueError, UnicodeError):
            raise DesktopError(INVALID) from None


def default_profile():
    # No environment override, production selector, or source-tree fallback.
    return TestProfile(Path.home() / "Library" / "Application Support" / "MarjanApp-TEST")


def configure_environment(profile):
    """Discard inherited deployment settings before importing Django."""
    for name in tuple(os.environ):
        if name.startswith(("MARJAN_", "DJANGO_")):
            del os.environ[name]
    os.environ.update({
        "DJANGO_SETTINGS_MODULE": "desktop.settings",
        "MARJAN_DB_PATH": str(profile.database),
        "MARJAN_MEDIA_ROOT": str(profile.root / "media"),
        "MARJAN_RENDERING_ROOT": str(profile.root / "rendered"),
        "DJANGO_DEBUG": "0",
        "DJANGO_SECRET_KEY": profile.load(),
    })


class ProfileLock:
    """Kernel lock survives crashes without stale PID heuristics. Never unlink it."""

    def __init__(self, profile):
        self.profile = profile
        self.stream = None

    def __enter__(self):
        path = self.profile.root / "desktop.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            self.profile.private(path)
            if os.name == "nt":
                import msvcrt

                if not os.fstat(descriptor).st_size:
                    self.stream.write(b"0")
                self.stream.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise DesktopError("MarjanApp TEST že deluje. Zaprite obstoječe okno pred ponovnim zagonom.") from None
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self, *args):
        self.stream.close()


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def new_secret():
    return secrets.token_urlsafe(64)


def require_test_archive_paths(paths):
    from django.conf import settings

    allowed = settings.MARJAN_TEST_ARCHIVE_PATHS
    # Compare spelling before resolving anything outside the TEST installation.
    if tuple(str(p) for p in paths) != tuple(str(p) for p in allowed[:len(paths)]):
        raise ValueError("Desktop TEST permits only its private synthetic archive locations.")
    profile = TestProfile(allowed[0].parent)
    for path in allowed:
        profile.private(path, directory=True)
