"""Serialize monthly transitions across local processes without a DB transaction."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import connection

from archive.backup import create_backup_snapshot
from archive.models import BackupSnapshot


@contextmanager
def _monthly_lock():
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ValidationError(
            "Mesečni prehod z varnostno kopijo ne sme potekati v transakciji baze."
        )
    identity = hashlib.sha256(
        str(Path(connection.settings_dict["NAME"]).resolve()).encode()
    ).hexdigest()
    path = Path(tempfile.gettempdir()) / f"marjanapp-monthly-{identity}.lock"
    with path.open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def monthly_backup(trigger):
    def decorate(operation):
        @wraps(operation)
        def wrapped(*args, **kwargs):
            from companies.models import Company
            from invoicing import services
            from invoicing.models import MonthlyCycle

            if trigger == BackupSnapshot.Trigger.PRE_MONTHLY:
                period = services.normaliziraj_obdobje(
                    args[0] if args else kwargs["obdobje"]
                )
                if (
                    services._poisci_nadaljevanje(period)
                    or not Company.objects.filter(aktivno=True).exists()
                ):
                    return operation(*args, **kwargs)
                with _monthly_lock():
                    # Recheck after waiting: a concurrent request may have started it.
                    result = None
                    if services._poisci_nadaljevanje(period) is None:
                        result = create_backup_snapshot(trigger=trigger)
                    cycle = operation(*args, **kwargs)
                    cycle.backup_result = result
                    return cycle
            cycle = args[0] if args else kwargs["cycle"]
            if (
                cycle.pk is None
                or MonthlyCycle.objects.get(pk=cycle.pk).status
                == MonthlyCycle.Status.ZAKLJUCEN
            ):
                return operation(*args, **kwargs)
            with _monthly_lock():
                was_completed = (
                    MonthlyCycle.objects.get(pk=cycle.pk).status
                    == MonthlyCycle.Status.ZAKLJUCEN
                )
                completed = operation(*args, **kwargs)
                if not was_completed:
                    completed.backup_result = create_backup_snapshot(trigger=trigger)
                return completed

        return wrapped

    return decorate
