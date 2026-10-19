import hashlib
import io
import json
import sqlite3
import stat
from datetime import date
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction

from archive import restore
from archive.backup import create_backup_snapshot
from archive.configuration import verify_and_save_archive_roots
from archive.models import ArchivedFile, BackupSnapshot
from companies.models import Company, Signatory
from invoicing.models import Invoice
from tests.invoicing.test_invoice_models import podatki_racuna

pytestmark = pytest.mark.django_db(transaction=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def snapshot(tmp_path, settings):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox", "media")]
    for root in roots:
        root.mkdir()
    settings.MEDIA_ROOT = roots[3]
    verify_and_save_archive_roots(*roots[:3])
    company = Company.objects.create(
        naziv="Recovery company",
        davcna_stevilka="15012557",
        logotip="logo.png",
        logotip_checksum=sha(b"logo"),
    )
    Signatory.objects.create(
        company=company,
        ime_priimek="Signer",
        velja_od=date(2026, 1, 1),
        podpis_slika="signatures/signature.png",
        podpis_checksum=sha(b"signature"),
    )
    invoice = Invoice.objects.create(**podatki_racuna(company))
    (roots[0] / "2026").mkdir()
    (roots[0] / "2026/invoice.pdf").write_bytes(b"invoice")
    (roots[3] / "logo.png").write_bytes(b"logo")
    (roots[3] / "signatures").mkdir()
    (roots[3] / "signatures/signature.png").write_bytes(b"signature")
    ArchivedFile.objects.create(
        invoice=invoice,
        artifact_type="PDF",
        status="VERIFIED",
        local_path=str(roots[0] / "2026/invoice.pdf"),
        icloud_path=str(roots[1] / "2026/invoice.pdf"),
        checksum=sha(b"invoice"),
    )
    result = create_backup_snapshot(trigger=BackupSnapshot.Trigger.MANUAL)
    assert result.success, result.message
    return Path(result.snapshot.path), roots


def recover(snapshot, target, source=None):
    path, roots = snapshot
    return restore.restore_backup(
        source or path,
        target,
        local_root=roots[0],
        icloud_root=roots[1],
        dropbox_root=roots[2],
    )


def rewrite(snapshot, tmp_path, *, replace=None, omit=(), extra=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "changed.zip"
    with ZipFile(snapshot[0]) as original, ZipFile(path, "w") as output:
        for entry in original.infolist():
            if entry.filename not in omit:
                data = original.read(entry)
                if replace:
                    data = replace(entry.filename, data)
                output.writestr(entry, data)
        for name, data in extra:
            if isinstance(name, str) and "\\" in name:
                # ZipInfo's constructor normalizes OS separators on Windows;
                # preserve the actual hostile filename on disk for this test.
                entry = ZipInfo()
                entry.filename = name
                entry.orig_filename = name
                name = entry
            output.writestr(name, data)
    return path


def live_bytes(snapshot):
    database = Path(connection.settings_dict["NAME"])
    paths = [database, *database.parent.glob(database.name + "-*"), snapshot[0]]
    for root in snapshot[1]:
        paths.extend(path for path in root.rglob("*") if path.is_file())
    return {path: path.read_bytes() for path in paths}


def assert_failed(snapshot, tmp_path, source=None, target=None):
    target = target or tmp_path / "recovered"
    before = live_bytes(snapshot)
    result = recover(snapshot, target, source)
    assert not result.success
    assert result.target_directory is None
    assert not target.exists()
    assert not list(tmp_path.glob(".marjanapp-restore-*"))
    assert live_bytes(snapshot) == before
    return result


def test_end_to_end_offline_immutable_restore(snapshot, tmp_path):
    before = live_bytes(snapshot)
    target = tmp_path / "recovered"

    def no_live_sql(*args, **kwargs):
        pytest.fail("Restore must never query live DB")

    with connection.execute_wrapper(no_live_sql):
        result = recover(snapshot, target)
    assert result.success, result.message
    assert result.target_directory == target
    assert {path.name for path in target.iterdir()} == {
        "database", "archive", "media", "rendering",
    }
    with ZipFile(snapshot[0]) as original:
        assert (target / "database/db.sqlite3").read_bytes() == original.read(
            "database/db.sqlite3"
        )
    with sqlite3.connect(target / "database/db.sqlite3") as database:
        assert database.execute("SELECT naziv FROM companies_company").fetchone() == (
            "Recovery company",
        )
        assert database.execute(
            "SELECT COUNT(*) FROM invoicing_invoice"
        ).fetchone() == (1,)
        assert database.execute(
            "SELECT local_root FROM archive_archiveconfiguration"
        ).fetchone() == (str(snapshot[1][0]),)
    for relative, original in (
        ("archive/2026/invoice.pdf", snapshot[1][0] / "2026/invoice.pdf"),
        ("media/logo.png", snapshot[1][3] / "logo.png"),
        ("media/signatures/signature.png", snapshot[1][3] / "signatures/signature.png"),
    ):
        assert (target / relative).read_bytes() == original.read_bytes()
    assert live_bytes(snapshot) == before


@pytest.mark.parametrize(
    "entry",
    ["archive/2026/invoice.pdf", "media/logo.png", "media/signatures/signature.png"],
)
@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_references_fail_closed(snapshot, tmp_path, entry, corrupt):
    source = rewrite(
        snapshot,
        tmp_path,
        replace=(lambda name, data: b"wrong" if name == entry else data)
        if corrupt
        else None,
        omit=() if corrupt else (entry,),
    )
    assert_failed(snapshot, tmp_path, source)


@pytest.mark.parametrize(
    "manifest",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"format_version":1,"format_version":1}',
        json.dumps({"format_version": 2}).encode(),
        b"x" * 16385,
    ],
)
def test_invalid_manifest(snapshot, tmp_path, manifest):
    source = rewrite(
        snapshot,
        tmp_path,
        replace=lambda name, data: manifest if name == "manifest.json" else data,
    )
    assert_failed(snapshot, tmp_path, source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_checksum", "0" * 64),
        ("database_checksum", "bad"),
        ("format_version", True),
        ("trigger", "UNKNOWN"),
        ("created_at", "2026-10-09T12:00:00"),
        ("created_at", "invalid"),
    ],
)
def test_manifest_fields_and_database_digest(snapshot, tmp_path, field, value):
    def replace(name, data):
        if name == "manifest.json":
            manifest = json.loads(data)
            manifest[field] = value
            return json.dumps(manifest).encode()
        return data

    assert_failed(snapshot, tmp_path, rewrite(snapshot, tmp_path, replace=replace))


@pytest.mark.parametrize(
    "kind", ["truncated", "crc", "database", "quick_check", "schema"]
)
def test_corrupt_zip_or_database(snapshot, tmp_path, kind):
    if kind in {"truncated", "crc"}:
        source = tmp_path / "broken.zip"
        if kind == "truncated":
            source.write_bytes(b"PK broken")
        else:
            with (
                ZipFile(source, "w", compression=ZIP_STORED) as output,
                ZipFile(snapshot[0]) as original,
            ):
                for entry in original.infolist():
                    output.writestr(entry.filename, original.read(entry))
            data = source.read_bytes()
            assert b"signature" in data
            source.write_bytes(data.replace(b"signature", b"SIGNATURE", 1))
    else:
        with ZipFile(snapshot[0]) as original:
            data = original.read("database/db.sqlite3")
        if kind == "database":
            data = b"not SQLite"
        elif kind == "quick_check":
            damaged = bytearray(data)
            damaged[100] = 255  # Invalid first b-tree page type.
            data = bytes(damaged)
        else:
            database_path = tmp_path / "other.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE unrelated (id INTEGER)")
            data = database_path.read_bytes()

        def replace(name, content):
            if name == "database/db.sqlite3":
                return data
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["database_checksum"] = sha(data)
                return json.dumps(manifest).encode()
            return content

        source = rewrite(snapshot, tmp_path, replace=replace)
    assert_failed(snapshot, tmp_path, source)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "archive/../escape",
        "archive\\escape",
        "C:/escape",
        "unknown/file",
        "archive/file:stream",
        "archive/CON",
        "archive/trailing.",
        "database/unexpected",
        "manifest.json/child",
    ],
)
def test_unsafe_or_unexpected_entry(snapshot, tmp_path, name):
    source = rewrite(snapshot, tmp_path, extra=[(name, b"unsafe")])
    assert_failed(snapshot, tmp_path, source)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK]
)
def test_symlinks_and_special_entries(snapshot, tmp_path, mode):
    entry = ZipInfo("archive/special")
    entry.create_system = 3
    entry.external_attr = (mode | 0o777) << 16
    assert_failed(
        snapshot, tmp_path, rewrite(snapshot, tmp_path, extra=[(entry, b"target")])
    )


@pytest.mark.parametrize(
    "entries",
    [
        [("archive/2026/invoice.pdf", b"duplicate")],
        [("archive/2026/INVOICE.pdf", b"collision")],
        [("Archive/new", b"collision")],
        [("archive/2026", b"file-directory collision")],
        [("archive/implicit/a", b"one"), ("archive/IMPLICIT/b", b"two")],
        [("archive/caf\u00e9", b"one"), ("archive/cafe\u0301", b"two")],
    ],
)
def test_duplicate_and_colliding_entries(snapshot, tmp_path, entries):
    # zipfile warns on deliberately duplicated names.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        source = rewrite(snapshot, tmp_path, extra=entries)
    assert_failed(snapshot, tmp_path, source)


@pytest.mark.parametrize("populated", [False, True])
def test_existing_target_rejected(snapshot, tmp_path, populated):
    target = tmp_path / "existing"
    target.mkdir()
    if populated:
        (target / "keep").write_bytes(b"untouched")
    before = live_bytes(snapshot)
    assert not recover(snapshot, target).success
    assert list(target.iterdir()) == ([target / "keep"] if populated else [])
    assert live_bytes(snapshot) == before


@pytest.mark.parametrize("index", range(4))
def test_live_roots_rejected(snapshot, tmp_path, index):
    assert_failed(snapshot, tmp_path, target=snapshot[1][index] / "recovery")


def test_live_database_rejected(snapshot, tmp_path, settings, monkeypatch):
    target = tmp_path / "recovery"
    before = live_bytes(snapshot)
    with monkeypatch.context() as patches:
        patches.setitem(
            settings.DATABASES["default"], "NAME", target / "database/db.sqlite3"
        )
        assert not recover(snapshot, target).success
    assert not target.exists()
    assert live_bytes(snapshot) == before


def test_current_and_snapshot_roots_both_protected(snapshot, tmp_path):
    new_roots = [tmp_path / name for name in ("new-local", "new-icloud", "new-dropbox")]
    for root in new_roots:
        root.mkdir()
    changed = snapshot[0], [*new_roots, snapshot[1][3]]
    assert not recover(changed, new_roots[0] / "recovery").success
    assert not recover(changed, snapshot[1][0] / "recovery").success
    assert not list(snapshot[1][0].glob(".marjanapp-restore-*"))


def test_transaction_rejected_before_io(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        restore, "_absolute_root", lambda *_: pytest.fail("No IO in transaction")
    )
    with transaction.atomic():
        assert not recover(snapshot, tmp_path / "recovery").success
    connection.set_autocommit(False)
    try:
        assert not recover(snapshot, tmp_path / "recovery").success
    finally:
        connection.set_autocommit(True)


def test_publish_failure_cleans_temporary_restore(snapshot, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("publish failed")

    monkeypatch.setattr(restore, "_publish_directory", fail)
    assert_failed(snapshot, tmp_path)


def test_competing_target_never_replaced(snapshot, tmp_path, monkeypatch):
    original = restore._publish_directory

    def competing(work, target):
        target.mkdir()
        original(work, target)

    monkeypatch.setattr(restore, "_publish_directory", competing)
    target = tmp_path / "recovered"
    assert not recover(snapshot, target).success
    assert target.is_dir() and not list(target.iterdir())
    assert not list(tmp_path.glob(".marjanapp-restore-*"))


def test_command_success_and_failure(snapshot, tmp_path):
    output = io.StringIO()
    kwargs = dict(
        zip(("local_root", "icloud_root", "dropbox_root"), map(str, snapshot[1][:3]))
    )
    before = live_bytes(snapshot)
    target = tmp_path / "command-recovery"
    call_command(
        "restore_backup_snapshot",
        str(snapshot[0]),
        str(target),
        stdout=output,
        **kwargs,
    )
    assert "restored and verified" in output.getvalue()
    assert target.is_dir()
    with pytest.raises(CommandError, match="already exists"):
        call_command("restore_backup_snapshot", str(snapshot[0]), str(target), **kwargs)
    assert live_bytes(snapshot) == before


def test_command_requires_current_roots(snapshot, tmp_path):
    with pytest.raises(CommandError):
        call_command(
            "restore_backup_snapshot", str(snapshot[0]), str(tmp_path / "recovery")
        )


def alter_database(snapshot, tmp_path, sql):
    database_path = tmp_path / "edited.sqlite3"
    with ZipFile(snapshot[0]) as original:
        database_path.write_bytes(original.read("database/db.sqlite3"))
    with sqlite3.connect(database_path) as database:
        database.execute(sql)
    data = database_path.read_bytes()

    def replace(name, content):
        if name == "database/db.sqlite3":
            return data
        if name == "manifest.json":
            manifest = json.loads(content)
            manifest["database_checksum"] = sha(data)
            return json.dumps(manifest).encode()
        return content

    return rewrite(snapshot, tmp_path, replace=replace)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM archive_archiveconfiguration",
        "UPDATE archive_archivedfile SET local_path='/outside/invoice.pdf'",
        "UPDATE archive_archivedfile SET checksum='invalid'",
        "UPDATE companies_company SET logotip='../outside.png'",
        "UPDATE companies_signatory SET podpis_slika='/absolute.png'",
        "UPDATE companies_company SET logotip_checksum='invalid'",
        "ALTER TABLE companies_company DROP COLUMN logotip_checksum",
        "UPDATE archive_archivedfile SET invoice_id=999999",
    ],
)
def test_invalid_recovered_metadata_fails_closed(snapshot, tmp_path, sql):
    assert_failed(snapshot, tmp_path, alter_database(snapshot, tmp_path, sql))


def test_assets_without_stored_checksum_still_require_presence(snapshot, tmp_path):
    source = alter_database(
        snapshot, tmp_path, "UPDATE companies_company SET logotip_checksum=''"
    )
    result = recover(snapshot, tmp_path / "without-checksum", source)
    assert result.success, result.message
    source = rewrite(
        (source, snapshot[1]), tmp_path / "missing", omit=("media/logo.png",)
    )
    assert_failed(snapshot, tmp_path, source)


def test_restore_without_available_live_database(
    snapshot, tmp_path, settings, monkeypatch
):
    connection.close()
    unavailable = tmp_path / "unavailable-live.sqlite3"
    with monkeypatch.context() as patches:
        patches.setitem(settings.DATABASES["default"], "NAME", unavailable)
        patches.setattr(
            connection,
            "ensure_connection",
            lambda: pytest.fail("No live connection allowed"),
        )
        result = recover(snapshot, tmp_path / "offline-recovery")
    assert result.success, result.message
    assert not unavailable.exists()


def test_streaming_extraction_failure_cleans_partial_temp(
    snapshot, tmp_path, monkeypatch
):
    def failed_copy(source, destination, **kwargs):
        destination.write(source.read(1))
        raise OSError("Interrupted extraction")

    monkeypatch.setattr(restore.shutil, "copyfileobj", failed_copy)
    assert_failed(snapshot, tmp_path)


def test_incomplete_backup_layout_rejected(snapshot, tmp_path):
    source = rewrite(snapshot, tmp_path, omit=("manifest.json",))
    assert_failed(snapshot, tmp_path, source)


def root_metadata(snapshot):
    return dict(
        zip(
            ("local_root", "icloud_root", "dropbox_root"),
            map(str, snapshot[1][:3]),
            strict=True,
        )
    )


def with_manifest_roots(snapshot, tmp_path, metadata):
    def replace(name, content):
        if name == "manifest.json":
            manifest = json.loads(content)
            if metadata is None:
                manifest.pop("source_roots")
            else:
                manifest["source_roots"] = metadata
            return json.dumps(manifest).encode()
        return content

    return rewrite(snapshot, tmp_path, replace=replace)


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("target_is_parent", [False, True])
def test_historical_overlap_rejected_before_sibling_or_extraction(
    snapshot,
    tmp_path,
    monkeypatch,
    index,
    target_is_parent,
):
    target = tmp_path / "historical-recovery"
    metadata = root_metadata(snapshot)
    roots = list(metadata)
    if target_is_parent:
        metadata[roots[index]] = str(target / "historical-root")
        # Keep SQLite configuration consistent with the manifest, including a
        # historical root that no longer exists on this host.
        database = tmp_path / "historical.sqlite3"
        with ZipFile(snapshot[0]) as archive:
            database.write_bytes(archive.read("database/db.sqlite3"))
        with sqlite3.connect(database) as copied:
            copied.execute(
                f"UPDATE archive_archiveconfiguration SET {roots[index]}=?",
                (metadata[roots[index]],),
            )
        database_bytes = database.read_bytes()

        def replace(name, content):
            if name == "database/db.sqlite3":
                return database_bytes
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["source_roots"] = metadata
                manifest["database_checksum"] = sha(database_bytes)
                return json.dumps(manifest).encode()
            return content

        source = rewrite(snapshot, tmp_path, replace=replace)
    else:
        target = snapshot[1][index] / "recovery"
        source = snapshot[0]
    current = [
        tmp_path / name
        for name in ("current-local", "current-icloud", "current-dropbox")
    ]
    for root in current:
        root.mkdir()
    before = live_bytes(snapshot)

    def forbidden(*args, **kwargs):
        pytest.fail("Historical overlap must fail before mkdtemp or extraction")

    monkeypatch.setattr(restore, "mkdtemp", forbidden)
    monkeypatch.setattr(restore.shutil, "copyfileobj", forbidden)
    result = restore.restore_backup(
        source,
        target,
        local_root=current[0],
        icloud_root=current[1],
        dropbox_root=current[2],
    )
    assert not result.success
    assert "overlaps" in result.message
    assert not target.exists()
    assert live_bytes(snapshot) == before
    assert not list(tmp_path.rglob(".marjanapp-restore-*"))


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        [],
        "invalid",
        {
            "local_root": "relative",
            "icloud_root": "/icloud",
            "dropbox_root": "/dropbox",
        },
        {
            "local_root": "/local/../escape",
            "icloud_root": "/icloud",
            "dropbox_root": "/dropbox",
        },
        {
            "local_root": "/local",
            "icloud_root": "/local/nested",
            "dropbox_root": "/dropbox",
        },
        {"local_root": None, "icloud_root": "/icloud", "dropbox_root": "/dropbox"},
        {
            "local_root": "/local",
            "icloud_root": "/icloud",
            "dropbox_root": "/dropbox",
            "extra": "/other",
        },
    ],
)
def test_missing_invalid_root_metadata_rejected_before_sibling(
    snapshot,
    tmp_path,
    monkeypatch,
    metadata,
):
    source = with_manifest_roots(snapshot, tmp_path, metadata)
    monkeypatch.setattr(
        restore, "mkdtemp", lambda **kwargs: pytest.fail("No sibling extraction")
    )
    assert_failed(snapshot, tmp_path, source)


@pytest.mark.parametrize("index", range(3))
def test_forged_manifest_roots_rejected_before_sibling(
    snapshot, tmp_path, monkeypatch, index
):
    metadata = root_metadata(snapshot)
    metadata[list(metadata)[index]] = str(tmp_path / "forged-root")
    source = with_manifest_roots(snapshot, tmp_path, metadata)
    before_scratch = set(
        Path(restore.gettempdir()).glob(".marjanapp-restore-preflight-*")
    )
    monkeypatch.setattr(
        restore, "mkdtemp", lambda **kwargs: pytest.fail("No sibling extraction")
    )
    result = assert_failed(snapshot, tmp_path, source)
    assert "contradict" in result.message
    assert (
        set(Path(restore.gettempdir()).glob(".marjanapp-restore-preflight-*"))
        == before_scratch
    )


def test_valid_restore_with_different_current_roots(snapshot, tmp_path):
    current = [
        tmp_path / name
        for name in ("current-local", "current-icloud", "current-dropbox")
    ]
    before = live_bytes(snapshot)
    result = restore.restore_backup(
        snapshot[0],
        tmp_path / "different-roots-recovery",
        local_root=current[0],
        icloud_root=current[1],
        dropbox_root=current[2],
    )
    assert result.success, result.message
    assert live_bytes(snapshot) == before
    assert not list(tmp_path.rglob(".marjanapp-restore-*"))


@pytest.mark.parametrize("index", range(3))
def test_forged_roots_cannot_enable_historical_sibling_writes(
    snapshot,
    tmp_path,
    monkeypatch,
    index,
):
    metadata = root_metadata(snapshot)
    metadata[list(metadata)[index]] = str(tmp_path / "forged-root")
    source = with_manifest_roots(snapshot, tmp_path, metadata)
    current = [tmp_path / name for name in ("new-local", "new-icloud", "new-dropbox")]
    before = live_bytes(snapshot)
    monkeypatch.setattr(
        restore, "mkdtemp", lambda **kwargs: pytest.fail("No historical sibling writes")
    )
    result = restore.restore_backup(
        source,
        snapshot[1][index] / "recovery",
        local_root=current[0],
        icloud_root=current[1],
        dropbox_root=current[2],
    )
    assert not result.success
    assert "contradict" in result.message
    assert live_bytes(snapshot) == before
    assert not list(tmp_path.rglob(".marjanapp-restore-*"))


def test_preflight_refuses_scratch_inside_protected_root(
    snapshot, tmp_path, monkeypatch
):
    monkeypatch.setattr(restore, "gettempdir", lambda: str(snapshot[1][0]))
    monkeypatch.setattr(
        restore,
        "NamedTemporaryFile",
        lambda **kwargs: pytest.fail("No scratch in protected root"),
    )
    result = assert_failed(snapshot, tmp_path)
    assert "safe offline database preflight" in result.message
