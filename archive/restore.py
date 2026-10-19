"""Offline recovery into a new directory; never reconfigure recovered data."""

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import NamedTemporaryFile, gettempdir, mkdtemp

from django.conf import settings
from django.db import connections

from .backup import _hash_file

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRIGGERS = {"MANUAL", "PRE_MONTHLY", "POST_MONTHLY", "SCHEDULED"}
_ROOT_NAMES = ("local_root", "icloud_root", "dropbox_root")
_SCHEMA = {
    "archive_archiveconfiguration": {"id", "local_root", "icloud_root", "dropbox_root"},
    "archive_archivedfile": {"local_path", "checksum", "status"},
    "companies_company": {"logotip", "logotip_checksum"},
    "companies_signatory": {"podpis_slika", "podpis_checksum"},
    "invoicing_invoice": {"id", "company_id", "status", "stevilka"},
    "django_migrations": {"app", "name"},
}


class RestoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RestoreResult:
    success: bool
    target_directory: Path | None
    message: str


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError("Duplicate manifest key.")
        result[key] = value
    return result


def read_manifest(archive):
    try:
        entry = archive.getinfo("manifest.json")
        if entry.file_size > 16 * 1024 * 1024:
            raise RestoreError("Manifest is too large.")
        manifest = json.loads(archive.read(entry), object_pairs_hook=_json_object)
        if not isinstance(manifest, dict):
            raise RestoreError("Invalid manifest.")
        if (
            type(manifest.get("format_version")) is not int
            or manifest["format_version"] not in {1, 2}
        ):
            raise RestoreError("Unsupported backup format.")
        if not isinstance(
            manifest.get("database_checksum"), str
        ) or not _SHA256.fullmatch(manifest["database_checksum"]):
            raise RestoreError("Invalid manifest database checksum.")
        if manifest.get("trigger") not in _TRIGGERS:
            raise RestoreError("Invalid manifest trigger.")
        created_at = datetime.fromisoformat(manifest["created_at"])
        if created_at.utcoffset() is None:
            raise RestoreError("Manifest timestamp must include a timezone.")
        _source_roots(manifest.get("source_roots"))
        if manifest["format_version"] == 2:
            _stored_path(manifest["rendering_root"])
            inventory = manifest["official_pdfs"]
            if not isinstance(inventory, list):
                raise RestoreError("Invalid official PDF inventory.")
            for item in inventory:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"invoice_id", "path", "checksum"}
                    or type(item["invoice_id"]) is not int
                    or item["invoice_id"] < 1
                    or not isinstance(item["checksum"], str)
                    or not _SHA256.fullmatch(item["checksum"])
                ):
                    raise RestoreError("Invalid official PDF inventory entry.")
                _relative_parts(item["path"])
        return manifest
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise RestoreError("Invalid or unsupported backup manifest.") from error


def _relative_parts(name):
    # Portable names avoid Windows aliases even when recovery runs on macOS.
    if not isinstance(name, str) or not name or "\\" in name:
        raise RestoreError("Unsafe file path.")
    parts = name.split("/")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in ':<>"|?*' for char in part)
            or part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
        ):
            raise RestoreError("Unsafe file path.")
    return tuple(parts)


def _entries(archive):
    manifest = read_manifest(archive)
    prefixes = {"manifest.json", "database", "archive", "media"}
    if manifest["format_version"] == 2:
        prefixes.add("rendering")
    entries = []
    explicit = set()
    nodes = {}
    for entry in archive.infolist():
        name = entry.filename
        if entry.orig_filename != name:
            raise RestoreError("ZIP filename contains a NUL byte.")
        parts = _relative_parts(name[:-1] if entry.is_dir() else name)
        if parts[0] not in prefixes:
            raise RestoreError("Unexpected backup content.")
        if parts[0] == "manifest.json" and (len(parts) != 1 or entry.is_dir()):
            raise RestoreError("Invalid manifest entry.")
        if (
            parts[0] == "database"
            and parts != ("database", "db.sqlite3")
            and (parts != ("database",) or not entry.is_dir())
        ):
            raise RestoreError("Unexpected database content.")
        if parts == ("database", "db.sqlite3") and entry.is_dir():
            raise RestoreError("Invalid database entry.")
        if parts[0] in {"archive", "media", "rendering"} and len(parts) == 1 and not entry.is_dir():
            raise RestoreError("Invalid backup tree.")
        mode = entry.external_attr >> 16
        kind = stat.S_IFMT(mode)
        expected = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
        if kind not in {0, expected} or entry.external_attr & 0x400:
            raise RestoreError("ZIP links and special files are forbidden.")
        if entry.is_dir() and entry.file_size:
            raise RestoreError("Directory entry contains data.")
        for index in range(1, len(parts) + 1):
            spelling = parts[:index]
            key = tuple(
                unicodedata.normalize("NFC", part).casefold() for part in spelling
            )
            is_directory = index < len(parts) or entry.is_dir()
            if key in nodes and nodes[key] != (spelling, is_directory):
                raise RestoreError("Colliding ZIP entries.")
            nodes[key] = (spelling, is_directory)
        if key in explicit:
            raise RestoreError("Duplicate ZIP entry.")
        explicit.add(key)
        entries.append((entry, parts))
    names = {entry.filename for entry, _parts in entries}
    if not {"manifest.json", "database/db.sqlite3", "archive/", "media/"} <= names:
        raise RestoreError("Incomplete backup layout.")
    if manifest["format_version"] == 2 and "rendering/" not in names:
        raise RestoreError("Missing official PDF backup tree.")
    return entries


def _absolute_root(value):
    path = Path(value)
    if not str(value).strip() or not path.is_absolute():
        raise RestoreError("Protected roots must be absolute paths.")
    return path.resolve()


def _protect(target, roots):
    if any(
        target.is_relative_to(root) or root.is_relative_to(target) for root in roots
    ):
        raise RestoreError("Recovery target overlaps a protected live location.")


def _stored_path(value):
    # Read snapshot paths lexically, including snapshots from another OS.
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise RestoreError("Invalid archived absolute path.")
    path = (
        PureWindowsPath(value)
        if PureWindowsPath(value).drive or "\\" in value
        else PurePosixPath(value)
    )
    if not path.is_absolute() or ".." in path.parts:
        raise RestoreError("Invalid archived absolute path.")
    return path


def _source_roots(metadata):
    if not isinstance(metadata, dict) or set(metadata) != set(_ROOT_NAMES):
        raise RestoreError("Missing or invalid source-root metadata.")
    roots = [_stored_path(metadata[name]) for name in _ROOT_NAMES]
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise RestoreError("Overlapping historical source roots.")
    return roots


def _protect_source_roots(target, metadata):
    for root in _source_roots(metadata):
        # Foreign OS paths cannot identify directories on this recovery host.
        if Path(str(root)).is_absolute():
            _protect(target, [Path(str(root)).resolve()])


def _database_source_roots(recovered):
    recovered.execute("PRAGMA trusted_schema=OFF")
    if recovered.execute(
        "SELECT type FROM sqlite_master WHERE name='archive_archiveconfiguration'"
    ).fetchone() != ("table",):
        raise RestoreError("Unrecognizable archived configuration schema.")
    configuration = recovered.execute(
        "SELECT local_root, icloud_root, dropbox_root FROM archive_archiveconfiguration WHERE id=1"
    ).fetchone()
    if configuration is None:
        raise RestoreError("Missing archived configuration.")
    metadata = dict(zip(_ROOT_NAMES, configuration, strict=True))
    _source_roots(metadata)
    return metadata


def snapshot_source_roots(database):
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as recovered:
        return _database_source_roots(recovered)


def snapshot_official_pdfs(database):
    """Derive the complete inventory from immutable captured SQLite, never ORM."""
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as recovered:
        recovered.execute("PRAGMA trusted_schema=OFF")
        if recovered.execute(
            "SELECT type FROM sqlite_master WHERE name='invoicing_invoice'"
        ).fetchone() != ("table",):
            raise RestoreError("Unrecognizable official PDF schema.")
        inventory = []
        for invoice_id, company_id, number, path, checksum in recovered.execute(
            "SELECT id, company_id, stevilka, rendered_pdf_path, checksum "
            "FROM invoicing_invoice WHERE rendered_pdf_path IS NULL "
            "OR checksum IS NULL OR rendered_pdf_path != '' OR checksum != '' ORDER BY id"
        ):
            if (
                type(invoice_id) is not int or invoice_id < 1
                or type(company_id) is not int or company_id < 1
                or not isinstance(number, str) or not number or ".." in number
                or not isinstance(checksum, str) or not _SHA256.fullmatch(checksum)
            ):
                raise RestoreError("Inconsistent registered official PDF metadata.")
            expected = f"company-{company_id}/{number}.pdf"
            if len(_relative_parts(expected)) != 2 or path != expected:
                raise RestoreError("Official PDF path does not match its invoice.")
            inventory.append({"invoice_id": invoice_id, "path": path, "checksum": checksum})
        return inventory


def verify_official_pdfs(archive, manifest, database):
    """Bind v2 inventory and exact ZIP PDF bytes to the captured database."""
    if manifest["official_pdfs"] != snapshot_official_pdfs(database):
        raise RestoreError("Official PDF inventory contradicts the snapshot database.")
    expected = {f"rendering/{item['path']}" for item in manifest["official_pdfs"]}
    actual = {entry.filename for entry in archive.infolist()
              if entry.filename.startswith("rendering/") and not entry.is_dir()}
    if actual != expected:
        raise RestoreError("Official PDF backup files differ from the inventory.")
    for item in manifest["official_pdfs"]:
        digest = hashlib.sha256()
        with archive.open(f"rendering/{item['path']}") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != item["checksum"]:
            raise RestoreError("Official PDF checksum differs from the snapshot database.")


def _protect_rendering_root(target, manifest):
    if manifest["format_version"] == 2:
        root = _stored_path(manifest["rendering_root"])
        if Path(str(root)).is_absolute():
            _protect(target, [Path(str(root)).resolve()])


def _preflight_roots(archive, manifest, target, protected):
    """Bind untrusted root metadata to SQLite before any sibling extraction.

    A streamed database-only scratch copy is kept outside known protected roots.
    This prevents a forged manifest from hiding an overlapping historical root.
    """
    temporary = None
    parent = Path(gettempdir()).resolve()
    historical = [
        Path(str(root)).resolve()
        for root in _source_roots(manifest["source_roots"])
        if Path(str(root)).is_absolute()
    ]
    if manifest["format_version"] == 2:
        rendering = _stored_path(manifest["rendering_root"])
        if Path(str(rendering)).is_absolute():
            historical.append(Path(str(rendering)).resolve())
    if any(parent.is_relative_to(root) for root in [*protected, *historical]):
        raise RestoreError("No safe offline database preflight directory.")
    try:
        with NamedTemporaryFile(
            dir=parent, prefix=".marjanapp-restore-preflight-", delete=False
        ) as output:
            temporary = Path(output.name)
            with archive.open("database/db.sqlite3") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        if _hash_file(temporary) != manifest["database_checksum"]:
            raise RestoreError("Manifest database checksum differs.")
        actual = snapshot_source_roots(temporary)
        if actual != manifest["source_roots"]:
            raise RestoreError(
                "Manifest source roots contradict the snapshot database."
            )
        _protect_source_roots(target, actual)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _verify_file(path, checksum, *, required_checksum):
    if not path.is_file():
        raise RestoreError("A database-referenced recovery file is missing.")
    if checksum or required_checksum:
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise RestoreError("Invalid referenced file checksum.")
        if _hash_file(path) != checksum:
            raise RestoreError("A database-referenced recovery file checksum differs.")


def _verify_database(work, target, source_roots):
    database = work / "database/db.sqlite3"
    # immutable read-only mode avoids journals, WAL and any snapshot mutation.
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as recovered:
        recovered.execute("PRAGMA trusted_schema=OFF")
        if recovered.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RestoreError("Recovered SQLite quick_check failed.")
        tables = dict(recovered.execute("SELECT name, type FROM sqlite_master"))
        for table, required in _SCHEMA.items():
            if tables.get(table) != "table":
                raise RestoreError("Unrecognizable recovery database schema.")
            columns = {
                row[1] for row in recovered.execute(f'PRAGMA table_info("{table}")')
            }
            if not required <= columns:
                raise RestoreError("Unrecognizable recovery database schema.")
        if recovered.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RestoreError("Recovered database contains broken foreign keys.")
        metadata = _database_source_roots(recovered)
        if metadata != source_roots:
            raise RestoreError(
                "Manifest source roots contradict the snapshot database."
            )
        stored_roots = _source_roots(metadata)
        _protect_source_roots(target, metadata)
        for local_path, checksum in recovered.execute(
            "SELECT local_path, checksum FROM archive_archivedfile WHERE status='VERIFIED'"
        ):
            path = _stored_path(local_path)
            try:
                relative = path.relative_to(stored_roots[0])
            except ValueError as error:
                raise RestoreError(
                    "Verified archive path is outside snapshot configuration."
                ) from error
            parts = _relative_parts(relative.as_posix())
            _verify_file(
                work.joinpath("archive", *parts), checksum, required_checksum=True
            )
        for table, field, checksum_field in (
            ("companies_company", "logotip", "logotip_checksum"),
            ("companies_signatory", "podpis_slika", "podpis_checksum"),
        ):
            for name, checksum in recovered.execute(
                f'SELECT "{field}", "{checksum_field}" FROM "{table}"'
            ):
                if name:
                    parts = _relative_parts(name)
                    _verify_file(
                        work.joinpath("media", *parts),
                        checksum,
                        required_checksum=False,
                    )


def _publish_directory(work, target):
    """Atomic no-replace directory rename, including competing empty targets."""
    if os.name == "nt":
        os.rename(work, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(work), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(
            -100, os.fsencode(work), -100, os.fsencode(target), 1
        )  # RENAME_NOREPLACE
    else:
        raise RestoreError("Atomic exclusive recovery publication is unsupported.")
    if result:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise RestoreError("Recovery target already exists.")
        raise OSError(code, "Recovery publication failed.")


def restore_backup(
    source_zip, target_directory, *, local_root, icloud_root, dropbox_root
):
    """Require all current roots explicitly; never query the live database."""
    from zipfile import ZipFile

    work = None
    try:
        for connection in connections.all(initialized_only=True):
            if connection.in_atomic_block or (
                connection.connection is not None and not connection.get_autocommit()
            ):
                raise RestoreError("Restore must run outside a database transaction.")
        raw_target = Path(target_directory)
        if raw_target.exists() or raw_target.is_symlink():
            raise RestoreError("Recovery target already exists.")
        target = raw_target.absolute().resolve()
        source = Path(source_zip).resolve(strict=True)
        roots = [
            _absolute_root(value)
            for value in (
                local_root,
                icloud_root,
                dropbox_root,
                settings.MEDIA_ROOT,
                settings.RENDERING_ROOT,
            )
        ]
        roots.extend([Path(settings.DATABASES["default"]["NAME"]).resolve(), source])
        _protect(target, roots)
        if not target.parent.is_dir():
            raise RestoreError("Recovery parent directory must already exist.")
        with ZipFile(source) as archive:
            entries = _entries(archive)
            if archive.testzip() is not None:
                raise RestoreError("ZIP CRC verification failed.")
            manifest = read_manifest(archive)
            _protect_source_roots(target, manifest["source_roots"])
            _protect_rendering_root(target, manifest)
            _preflight_roots(archive, manifest, target, roots)
            work = Path(mkdtemp(prefix=".marjanapp-restore-", dir=target.parent))
            for entry, parts in entries:
                if entry.filename == "manifest.json":
                    continue
                destination = work.joinpath(*parts)
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.open(entry) as incoming,
                        destination.open("xb") as outgoing,
                    ):
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            checksum = _hash_file(work / "database/db.sqlite3")
            if checksum != manifest["database_checksum"]:
                raise RestoreError("Manifest database checksum differs.")
            _verify_database(work, target, manifest["source_roots"])
            if manifest["format_version"] == 2:
                verify_official_pdfs(archive, manifest, work / "database/db.sqlite3")
                for item in manifest["official_pdfs"]:
                    _verify_file(
                        work / "rendering" / item["path"], item["checksum"],
                        required_checksum=True,
                    )
            if _hash_file(work / "database/db.sqlite3") != checksum:
                raise RestoreError("Recovered database changed during verification.")
        _protect(target, roots)
        _protect_source_roots(target, manifest["source_roots"])
        _protect_rendering_root(target, manifest)
        if (
            target.parent.resolve() != work.parent
            or target.exists()
            or target.is_symlink()
        ):
            raise RestoreError("Recovery target changed during verification.")
        _publish_directory(work, target)
        work = None
        message = "Backup restored and verified in a new recovery directory."
        if manifest["format_version"] == 1:
            message += " Legacy v1: official PDF completeness is not verified."
        return RestoreResult(True, target, message)
    except RestoreError as error:
        return RestoreResult(False, None, str(error))
    except Exception:  # noqa: BLE001 - untrusted input must fail closed
        return RestoreResult(
            False,
            None,
            "Backup restore or verification failed; no recovery was published.",
        )
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
