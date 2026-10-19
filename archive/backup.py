"""Verified, append-only filesystem snapshots."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory, gettempdir
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .configuration import resolve_archive_locations
from .models import BackupSnapshot

FAILURE_MESSAGE = "Varnostne kopije ni bilo mogoče ustvariti in preveriti. Preverite lokacije in dostopnost datotek."


@dataclass(frozen=True, slots=True)
class BackupResult:
    success: bool
    snapshot: BackupSnapshot | None
    message: str


class BackupError(ValueError):
    pass


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_database(target):
    # SQLite's online backup API includes committed WAL pages and provides a
    # consistent database even when another connection writes during the copy.
    connection.ensure_connection()
    deadline = time.monotonic() + 60

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise BackupError("SQLite snapshot timed out.")

    with closing(sqlite3.connect(target)) as destination:
        connection.connection.backup(destination, pages=256, progress=progress)
        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise BackupError("Invalid SQLite snapshot.")
    with target.open("r+b") as source:
        os.fsync(source.fileno())


def _validate_sources(roots):
    media = Path(settings.MEDIA_ROOT)
    if not media.is_absolute():
        raise BackupError("MEDIA_ROOT must be absolute.")
    media = media.resolve(strict=True)
    rendering = Path(settings.RENDERING_ROOT)
    if not rendering.is_absolute():
        raise BackupError("RENDERING_ROOT must be absolute.")
    rendering = rendering.resolve()
    if rendering.exists() and not rendering.is_dir():
        raise BackupError("RENDERING_ROOT must be a directory.")
    paths = [roots.local_root, roots.icloud_root, roots.dropbox_root, media, rendering]
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise BackupError("Backup sources and destination must not overlap.")
    # Never let scratch databases become files in a copied source tree.
    if any(Path(gettempdir()).resolve().is_relative_to(root) for root in paths):
        raise BackupError("Temporary backup storage overlaps a protected root.")
    # iCloud is checked lexically for overlap, never read during backup.
    for path in (roots.local_root, roots.dropbox_root, media):
        if not path.is_dir():
            raise BackupError("Missing directory.")
    database = Path(connection.settings_dict["NAME"]).resolve()
    if any(database.is_relative_to(root) for root in paths):
        raise BackupError("Live database must not be included as an archive file.")
    return media, rendering


def _add_pdf(output, root, artifact):
    """Copy only a canonical registered PDF, rejecting links and changing files."""
    path = root
    for part in ("", *Path(artifact["path"]).parts):
        path = path / part
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise BackupError("Linked official PDF input is forbidden.")
        if not path.resolve(strict=True).is_relative_to(root):
            raise BackupError("Official PDF escaped its root.")
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError("Official PDF is not a regular file.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not os.path.samestat(metadata, before):
            raise BackupError("Official PDF changed before copying.")
        _reject_live_database(before)
        with output.open(f"rendering/{artifact['path']}", "w") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        after = os.fstat(source.fileno())
        if (
            (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or not os.path.samestat(before, path.lstat())
            or not path.resolve(strict=True).is_relative_to(root)
        ):
            raise BackupError("Official PDF changed during copying.")


def _reject_live_database(metadata):
    database = str(connection.settings_dict["NAME"])
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            current = Path(database + suffix).stat()
        except FileNotFoundError:
            continue
        if os.path.samestat(metadata, current):
            raise BackupError("Live SQLite files must not be copied as artifacts.")


def _add_tree(output, root, prefix):
    def failed(error):
        raise error

    output.writestr(f"{prefix}/", b"")
    for directory, directories, filenames in os.walk(
        root, onerror=failed, followlinks=False
    ):
        directory = Path(directory)
        if directory.is_symlink() or not directory.resolve(strict=True).is_relative_to(
            root
        ):
            raise BackupError("Directory escaped its root.")
        directories.sort()
        for name in sorted([*directories, *filenames]):
            path = directory / name
            relative = path.relative_to(root)
            if any(
                part in {".", ".."} or "\\" in part or ":" in part
                for part in relative.parts
            ):
                raise BackupError("Unsafe archive entry.")
            metadata = path.lstat()
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise BackupError("Linked input is not safe to include.")
            entry = f"{prefix}/{relative.as_posix()}"
            if stat.S_ISDIR(metadata.st_mode):
                output.writestr(f"{entry}/", b"")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupError("Input is not a regular file.")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or not os.path.samestat(
                    metadata, before
                ):
                    raise BackupError("Input changed during traversal.")
                if not path.resolve(strict=True).is_relative_to(root):
                    raise BackupError("Input escaped its root.")
                _reject_live_database(before)
                with output.open(entry, "w") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise BackupError("Input changed during backup.")


def _verify_zip(path, database_checksum):
    from .restore import _entries, read_manifest, verify_official_pdfs

    with ZipFile(path) as archive:
        _entries(archive)
        if archive.testzip() is not None:
            raise BackupError("ZIP verification failed.")
        manifest = read_manifest(archive)
        if manifest["database_checksum"] != database_checksum:
            raise BackupError("Manifest database checksum differs.")
        digest = hashlib.sha256()
        with archive.open("database/db.sqlite3") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != database_checksum:
            raise BackupError("Database checksum differs.")
        if manifest["format_version"] != 2:
            raise BackupError("New snapshots require official PDF verification.")
        with TemporaryDirectory(prefix="marjanapp-backup-check-") as work:
            database = Path(work) / "db.sqlite3"
            with archive.open("database/db.sqlite3") as source, database.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            if _hash_file(database) != database_checksum:
                raise BackupError("Verification database checksum differs.")
            verify_official_pdfs(archive, manifest, database)


def _fsync_directory(path):
    # Directory fsync is supported on macOS/POSIX, but not by Windows.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(temporary, root, created_at):
    timestamp = timezone.localtime(created_at, timezone.get_default_timezone()).replace(
        microsecond=0
    )
    while True:
        final = root / timestamp.strftime("%Y-%m-%d_%H%M%S_backup.zip")
        try:
            # Same-filesystem hard link atomically publishes the complete ZIP,
            # failing if another process already owns that filename.
            os.link(temporary, final)
            return final
        except FileExistsError:
            timestamp += timedelta(seconds=1)


def create_backup_snapshot(*, trigger):
    """Return safe feedback; persist metadata only after final verification."""
    if connection.in_atomic_block or not connection.get_autocommit():
        return BackupResult(False, None, FAILURE_MESSAGE)
    if trigger not in BackupSnapshot.Trigger.values or connection.vendor != "sqlite":
        return BackupResult(False, None, FAILURE_MESSAGE)
    temporary = None
    final = None
    persisted = False
    try:
        roots = resolve_archive_locations()
        media, rendering = _validate_sources(roots)
        created_at = timezone.now()
        with TemporaryDirectory(prefix="marjanapp-backup-") as work:
            database = Path(work) / "db.sqlite3"
            _copy_database(database)
            database_checksum = _hash_file(database)
            from .restore import snapshot_official_pdfs, snapshot_source_roots

            source_roots = snapshot_source_roots(database)
            if source_roots != {
                name: str(getattr(roots, name))
                for name in ("local_root", "icloud_root", "dropbox_root")
            }:
                raise BackupError("Snapshot configuration changed during capture.")
            official_pdfs = snapshot_official_pdfs(database)
            with NamedTemporaryFile(
                dir=roots.dropbox_root, prefix=".marjanapp-backup-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                with ZipFile(
                    stream, "w", compression=ZIP_DEFLATED, allowZip64=True
                ) as output:
                    output.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "format_version": 2,
                                "rendering_root": str(rendering),
                                "official_pdfs": official_pdfs,
                                "database_checksum": database_checksum,
                                "created_at": created_at.isoformat(),
                                "trigger": trigger,
                                "source_roots": source_roots,
                            },
                            sort_keys=True,
                        ),
                    )
                    output.write(database, "database/db.sqlite3")
                    _add_tree(output, roots.local_root, "archive")
                    _add_tree(output, media, "media")
                    output.writestr("rendering/", b"")
                    for artifact in official_pdfs:
                        _add_pdf(output, rendering, artifact)
                stream.flush()
                os.fsync(stream.fileno())
            _verify_zip(temporary, database_checksum)
            expected_zip_checksum = _hash_file(temporary)
            final = _publish(temporary, roots.dropbox_root, created_at)
            _fsync_directory(roots.dropbox_root)
            zip_checksum = _hash_file(final)
            if zip_checksum != expected_zip_checksum:
                raise BackupError("Published ZIP checksum differs.")
            _verify_zip(final, database_checksum)
            if _hash_file(final) != zip_checksum:
                raise BackupError("Published ZIP changed during verification.")
        with transaction.atomic():
            snapshot = BackupSnapshot.objects.create(
                path=str(final),
                zip_checksum=zip_checksum,
                database_checksum=database_checksum,
                created_at=created_at,
                verified_at=timezone.now(),
                trigger=trigger,
            )
        persisted = True
        return BackupResult(
            True, snapshot, "Varnostna kopija je ustvarjena in preverjena."
        )
    except Exception:  # noqa: BLE001 - no arbitrary filesystem details in feedback
        return BackupResult(False, None, FAILURE_MESSAGE)
    finally:
        for path in (temporary, final if not persisted else None):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
