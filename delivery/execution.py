"""Local, crash-released SMTP execution ownership."""

import errno
import os
from contextlib import contextmanager
from pathlib import Path

from django.db import connection


@contextmanager
def delivery_execution_lock(attempt_id):
    with _execution_lock(str(int(attempt_id))) as acquired:
        yield acquired


@contextmanager
def report_scope_execution_lock(kind, identity):
    """Separate report namespaces from invoice attempt IDs."""
    if kind not in ("monthly", "accountant") or not all(
        character.isdigit() or character == "-" for character in str(identity)
    ):
        raise ValueError("Invalid report execution identity.")
    with _execution_lock(f"{kind}-{identity}") as acquired:
        yield acquired


@contextmanager
def _execution_lock(name):
    # Keep lock files: unlinking allows two owners to lock different inodes.
    database = Path(connection.settings_dict["NAME"]).resolve()
    directory = database.parent / ".smtp-locks" / database.name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            def acquire():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)

        try:
            acquire()
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            yield False
        else:
            try:
                yield True
            finally:
                release()
