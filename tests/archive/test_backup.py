import hashlib
import io
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client

from archive import backup
from archive.configuration import (
    ArchiveConfigurationError,
    resolve_archive_locations,
    verify_and_save_archive_roots,
)
from archive.models import ArchiveConfiguration, ArchivedFile, BackupSnapshot
from companies.models import InitialSetupCompletion, NumberSequence
from companies.readiness import complete_initial_setup, initial_setup_completed
from companies.services import dodeli_naslednjo_stevilko
from delivery.models import DeliveryAttempt, MonthlySummaryAttempt
from invoicing import services
from invoicing.models import Invoice, InvoiceLine, MonthlyCycle
from tests.companies.test_setup_readiness import (
    five_issuers as five_issuers,  # noqa: PLC0414 -- Imported pytest fixture.
)
from tests.ui.test_company_confirmations import (
    confirmation_ui_data as confirmation_ui_data,  # noqa: PLC0414 -- Imported fixture.
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup_data(confirmation_ui_data, settings, tmp_path):
    # Backup sources are disjoint; shared UI fixtures nest rendering under media.
    media = tmp_path / "media"
    media.mkdir()
    for name in ("logo.png", "podpis.png"):
        (media / name).write_bytes((tmp_path / name).read_bytes())
    settings.MEDIA_ROOT = media
    return confirmation_ui_data


@pytest.fixture
def locations(tmp_path, settings):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox", "media")]
    for root in roots:
        root.mkdir()
    local, icloud, dropbox, media = roots
    settings.MEDIA_ROOT = media
    verify_and_save_archive_roots(local, icloud, dropbox)
    (local / "2026").mkdir()
    (local / "2026" / "invoice.pdf").write_bytes(b"local PDF")
    (icloud / "duplicate.pdf").write_bytes(b"iCloud duplicate")
    (dropbox / "old-backup.zip").write_bytes(b"older snapshot")
    (media / "logos").mkdir()
    (media / "logos" / "company.png").write_bytes(b"uploaded logo")
    (media / "signature.png").write_bytes(b"uploaded signature")
    (media / "attachments").mkdir()
    (media / "attachments" / "lease.pdf").write_bytes(b"uploaded attachment")
    (tmp_path / "repository-secret.txt").write_text("outside media")
    return roots


def create():
    return backup.create_backup_snapshot(trigger=BackupSnapshot.Trigger.MANUAL)


def test_snapshot_content_checksums_and_sqlite_integrity(locations, tmp_path):
    result = create()
    assert result.success, result.message
    snapshot = BackupSnapshot.objects.get()
    assert snapshot.verified_at >= snapshot.created_at
    path = Path(snapshot.path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == snapshot.zip_checksum
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert names == {
            "manifest.json",
            "rendering/",
            "database/db.sqlite3",
            "archive/",
            "archive/2026/",
            "archive/2026/invoice.pdf",
            "media/",
            "media/logos/",
            "media/logos/company.png",
            "media/signature.png",
            "media/attachments/",
            "media/attachments/lease.pdf",
        }
        data = archive.read("database/db.sqlite3")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest == {
            "format_version": 2,
            "rendering_root": str(tmp_path / "rendered"),
            "official_pdfs": [],
            "database_checksum": snapshot.database_checksum,
            "created_at": snapshot.created_at.isoformat(),
            "trigger": snapshot.trigger,
            "source_roots": dict(
                zip(
                    ("local_root", "icloud_root", "dropbox_root"),
                    map(str, locations[:3]),
                    strict=True,
                )
            ),
        }
        assert hashlib.sha256(data).hexdigest() == snapshot.database_checksum
        database = tmp_path / "checked.sqlite3"
        database.write_bytes(data)
    with sqlite3.connect(database) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        # Metadata is saved after copying and verification, not inside the backup.
        assert copied.execute(
            "SELECT COUNT(*) FROM archive_backupsnapshot"
        ).fetchone() == (0,)
    assert not list(locations[2].glob(".marjanapp-backup-*"))


def test_filename_collision_never_overwrites_and_uses_local_time(
    locations, monkeypatch
):
    frozen = datetime(2026, 10, 8, 10, 22, 33, tzinfo=UTC)
    monkeypatch.setattr(backup.timezone, "now", lambda: frozen)
    first = create()
    before = Path(first.snapshot.path).read_bytes()
    second = create()
    assert first.success and second.success
    assert Path(first.snapshot.path).name == "2026-10-08_122233_backup.zip"
    assert Path(second.snapshot.path).name == "2026-10-08_122234_backup.zip"
    assert Path(first.snapshot.path).read_bytes() == before
    assert BackupSnapshot.objects.count() == 2


@pytest.mark.parametrize(
    "stage",
    [
        "_copy_database",
        "_add_tree",
        "_hash_file",
        "_verify_zip",
        "_publish",
        "_fsync_directory",
    ],
)
def test_io_runs_without_database_transaction(locations, monkeypatch, stage):
    original = getattr(backup, stage)
    calls = []

    def checked(*args, **kwargs):
        assert not connection.in_atomic_block
        assert connection.get_autocommit()
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, stage, checked)
    assert create().success
    assert calls


def test_transaction_refuses_before_any_filesystem_work(locations, monkeypatch):
    monkeypatch.setattr(
        backup, "resolve_archive_locations", lambda: pytest.fail("No IO in transaction")
    )
    with transaction.atomic():
        assert not create().success
    connection.set_autocommit(False)
    try:
        assert not create().success
    finally:
        connection.set_autocommit(True)
    assert not BackupSnapshot.objects.exists()


@pytest.mark.parametrize(
    "stage",
    [
        "_copy_database",
        "_add_tree",
        "_hash_file",
        "_verify_zip",
        "_publish",
        "_fsync_directory",
    ],
)
def test_failure_has_no_row_or_new_final_zip_and_safe_feedback(
    locations, monkeypatch, stage
):
    def fail(*args, **kwargs):
        raise OSError("secret /filesystem/password")

    monkeypatch.setattr(backup, stage, fail)
    result = create()
    assert not result.success
    assert "secret" not in result.message
    assert not BackupSnapshot.objects.exists()
    assert [p.name for p in locations[2].iterdir()] == ["old-backup.zip"]


def test_published_corruption_is_detected_and_removed(locations, monkeypatch):
    original = backup._publish

    def corrupt(*args):
        path = original(*args)
        path.write_bytes(b"not a ZIP")
        return path

    monkeypatch.setattr(backup, "_publish", corrupt)
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert [p.name for p in locations[2].iterdir()] == ["old-backup.zip"]


def test_database_checksum_mismatch_fails_verification(locations):
    result = create()
    with pytest.raises(backup.BackupError):
        backup._verify_zip(result.snapshot.path, "0" * 64)


def test_unreadable_input_fails_closed(locations, monkeypatch):
    original = backup.os.open

    def denied(path, *args, **kwargs):
        if Path(path).name == "lease.pdf":
            raise PermissionError("private details")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(backup.os, "open", denied)
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert [p.name for p in locations[2].iterdir()] == ["old-backup.zip"]


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "file",
        "unreadable",
        "inside_dropbox",
        "inside_local",
        "inside_icloud",
    ],
)
def test_invalid_media_root_fails_closed(locations, settings, monkeypatch, kind):
    if kind == "missing":
        settings.MEDIA_ROOT = locations[3] / "missing"
    elif kind == "file":
        settings.MEDIA_ROOT = locations[3] / "signature.png"
    elif kind.startswith("inside_"):
        root = (
            locations[
                {"inside_dropbox": 2, "inside_local": 0, "inside_icloud": 1}[kind]
            ]
            / "assets"
        )
        root.mkdir()
        settings.MEDIA_ROOT = root
    else:
        original = backup.os.walk

        def denied(root, *args, **kwargs):
            if Path(root) == locations[3]:
                kwargs["onerror"](PermissionError("unreadable media"))
            return original(root, *args, **kwargs)

        monkeypatch.setattr(backup.os, "walk", denied)
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert [p.name for p in locations[2].iterdir() if p.is_file()] == ["old-backup.zip"]


def test_symlink_input_cannot_escape(locations, tmp_path):
    link = locations[0] / "escaped"
    try:
        link.symlink_to(tmp_path / "repository-secret.txt")
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    assert not create().success
    assert not BackupSnapshot.objects.exists()


def test_online_sqlite_backup_with_mutation(locations, monkeypatch, tmp_path):
    # Intercept the connection's backup call, not a raw-file-copy helper.
    connection.ensure_connection()
    source = connection.connection
    source.execute("CREATE TABLE backup_mutation_test (value TEXT)")
    source.executemany(
        "INSERT INTO backup_mutation_test VALUES (?)", [("x" * 4096,)] * 400
    )
    calls = []

    class Proxy:
        def __getattr__(self, name):
            return getattr(source, name)

        def backup(self, destination, **kwargs):
            original = kwargs["progress"]

            def progress(*args):
                if not calls:
                    with sqlite3.connect(connection.settings_dict["NAME"]) as writer:
                        writer.execute(
                            "INSERT INTO backup_mutation_test VALUES ('during backup')"
                        )
                    calls.append(True)
                original(*args)

            source.backup(destination, **{**kwargs, "progress": progress})

    monkeypatch.setattr(connection, "connection", Proxy())
    try:
        result = create()
        assert result.success, result.message
        assert calls
        with ZipFile(result.snapshot.path) as archive:
            copied = tmp_path / "mutated.sqlite3"
            copied.write_bytes(archive.read("database/db.sqlite3"))
        with sqlite3.connect(copied) as database:
            assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert database.execute(
                "SELECT COUNT(*) FROM backup_mutation_test"
            ).fetchone() == (401,)
    finally:
        monkeypatch.setattr(connection, "connection", source)
        source.execute("DROP TABLE backup_mutation_test")


def test_snapshot_metadata_is_immutable(locations):
    snapshot = create().snapshot
    old_checksum = snapshot.zip_checksum
    snapshot.zip_checksum = "0" * 64
    for operation in (
        snapshot.save,
        snapshot.delete,
        lambda: BackupSnapshot.objects.filter(pk=snapshot.pk).update(path="elsewhere"),
        lambda: BackupSnapshot.objects.all().delete(),
        lambda: BackupSnapshot.objects.bulk_update([snapshot], ["zip_checksum"]),
        lambda: BackupSnapshot.objects.bulk_create(
            [snapshot],
            update_conflicts=True,
            update_fields=["path"],
            unique_fields=["id"],
        ),
    ):
        with pytest.raises(ValidationError):
            operation()
    snapshot.refresh_from_db()
    assert snapshot.zip_checksum == old_checksum


def test_manual_ui_csrf_prg_get_does_not_backup(locations):
    client = Client(enforce_csrf_checks=True)
    page = client.get("/nastavitve/arhiv/")
    assert page.status_code == 200
    assert not BackupSnapshot.objects.exists()
    assert "Ustvari varnostno kopijo" in page.content.decode()
    assert (
        client.post("/nastavitve/arhiv/", {"action": "create_backup"}).status_code
        == 403
    )
    response = client.post(
        "/nastavitve/arhiv/",
        {
            "action": "create_backup",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    assert response.url == "/nastavitve/arhiv/?backup=success"
    page = client.get(response.url)
    assert "Zadnja uspešna kopija" in page.content.decode()
    assert BackupSnapshot.objects.get().trigger == BackupSnapshot.Trigger.MANUAL
    client.get(response.url)
    assert BackupSnapshot.objects.count() == 1
    assert "Obnovi" not in page.content.decode()


def test_manual_failure_is_safe_prg(locations, client, monkeypatch):
    monkeypatch.setattr(
        backup, "_copy_database", lambda *_: (_ for _ in ()).throw(OSError("secret"))
    )
    response = client.post("/nastavitve/arhiv/", {"action": "create_backup"})
    assert response.url == "/nastavitve/arhiv/?backup=failed"
    assert "secret" not in client.get(response.url).content.decode()
    assert not BackupSnapshot.objects.exists()


def test_management_command_scheduled_snapshot(locations):
    output = io.StringIO()
    call_command("create_backup_snapshot", stdout=output)
    assert BackupSnapshot.objects.get().trigger == BackupSnapshot.Trigger.SCHEDULED
    assert "preverjena" in output.getvalue()


def test_failed_dropbox_probe_preserves_config(locations, tmp_path):
    before = ArchiveConfiguration.objects.get()
    with pytest.raises(ArchiveConfigurationError, match="Dropbox"):
        verify_and_save_archive_roots(
            locations[0], locations[1], tmp_path / "missing-dropbox"
        )
    after = ArchiveConfiguration.objects.get()
    assert after.updated_at == before.updated_at
    assert after.dropbox_root == before.dropbox_root
    assert not list(locations[0].glob(".marjanapp-verify-*"))
    assert not list(locations[1].glob(".marjanapp-verify-*"))


@pytest.mark.parametrize("pair", [(0, 1), (0, 2), (1, 2)])
def test_all_root_pairs_are_distinct(locations, pair):
    roots = locations[:3]
    roots[pair[1]] = roots[pair[0]]
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(*roots)


def test_legacy_configuration_requires_dropbox_probe_for_readiness(locations):
    ArchiveConfiguration.objects.update(dropbox_root="")
    with pytest.raises(ArchiveConfigurationError):
        resolve_archive_locations()
    assert not create().success


def test_delete_probe_failure_preserves_config(locations, monkeypatch):
    before = ArchiveConfiguration.objects.get().verified_at
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.name.startswith(".marjanapp-verify-") and not kwargs.get("missing_ok"):
            raise OSError("failed delete")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(*locations[:3])
    assert ArchiveConfiguration.objects.get().verified_at == before
    assert not list(locations[0].glob(".marjanapp-verify-*"))


def test_media_root_default_and_env_override(monkeypatch, tmp_path):
    import runpy

    monkeypatch.delenv("MARJAN_MEDIA_ROOT", raising=False)
    default = runpy.run_path(str(Path(__file__).parents[2] / "app/settings.py"))
    assert default["MEDIA_ROOT"] == (default["BASE_DIR"] / "media").resolve()
    monkeypatch.setenv("MARJAN_MEDIA_ROOT", str(tmp_path / "persistent-assets"))
    overridden = runpy.run_path(str(Path(__file__).parents[2] / "app/settings.py"))
    assert overridden["MEDIA_ROOT"] == (tmp_path / "persistent-assets").resolve()


def test_pre_monthly_once_before_new_cycle_and_not_continuation(locations, monkeypatch):
    from tests.invoicing.test_monthly_cycle import ustvari_podjetje

    ustvari_podjetje("Backup company", "12345679")
    original = backup._copy_database
    observations = []

    def check_before(path):
        assert not MonthlyCycle.objects.exists()
        observations.append(True)
        original(path)

    monkeypatch.setattr(backup, "_copy_database", check_before)
    cycle = services.shrani_prvi_korak(date(2026, 10, 1), date(2026, 10, 8))
    assert cycle.backup_result.success
    assert observations == [True]
    assert BackupSnapshot.objects.get().trigger == BackupSnapshot.Trigger.PRE_MONTHLY
    assert services.shrani_prvi_korak(cycle.obdobje, cycle.datum_izdaje).pk == cycle.pk
    assert (
        services.zacni_ali_nadaljuj_mesecni_cikel(cycle.obdobje, cycle.datum_izdaje).pk
        == cycle.pk
    )
    assert BackupSnapshot.objects.count() == 1


def test_post_monthly_once_after_successful_finalization(locations, monkeypatch):
    from tests.invoicing.test_monthly_cycle_finalization import (
        _cycle,
        _successful,
        _summary,
    )

    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    original = backup._copy_database

    def check_completed(path):
        assert (
            MonthlyCycle.objects.get(pk=cycle.pk).status
            == MonthlyCycle.Status.ZAKLJUCEN
        )
        assert not connection.in_atomic_block
        original(path)

    monkeypatch.setattr(backup, "_copy_database", check_completed)
    completed = services.zakljuci_mesecni_cikel(cycle)
    assert completed.backup_result.success
    assert BackupSnapshot.objects.get().trigger == BackupSnapshot.Trigger.POST_MONTHLY
    services.zakljuci_mesecni_cikel(cycle)
    Client().get("/mesecni-obracun/")
    Client().get("/")
    assert BackupSnapshot.objects.count() == 1


def test_monthly_backup_failures_preserve_start_and_completed_state(
    five_issuers, monkeypatch, client, settings
):
    from archive.services import ArchiveRoots, archive_pdf
    from rendering.services import render_pdf
    from tests.companies.test_setup_readiness import issue_np
    from tests.invoicing.test_monthly_archive import _mark_sent
    from tests.invoicing.test_monthly_cycle_finalization import _successful, _summary

    data, _ = five_issuers
    complete_initial_setup()
    assert initial_setup_completed()
    invoice = issue_np(data)
    render_pdf(invoice)
    invoice.refresh_from_db()
    _mark_sent(invoice)
    roots = resolve_archive_locations()
    assert archive_pdf(invoice, ArchiveRoots(roots.local_root, roots.icloud_root)).success
    assert create().success

    def protected_state():
        rows = [list(model.objects.order_by("pk").values()) for model in (
            Invoice, InvoiceLine, NumberSequence, DeliveryAttempt, ArchivedFile,
            BackupSnapshot, MonthlySummaryAttempt, InitialSetupCompletion,
        )]
        files = {
            str(path): path.read_bytes()
            for root in (
                roots.local_root, roots.icloud_root, roots.dropbox_root,
                Path(settings.MEDIA_ROOT), Path(settings.RENDERING_ROOT),
            )
            for path in root.rglob("*") if path.is_file()
        }
        return rows, files

    attempts = []

    def fail_copy(*_):
        attempts.append(MonthlyCycle.objects.filter(status=MonthlyCycle.Status.ZAKLJUCEN).exists())
        raise OSError("secret /filesystem/password")

    monkeypatch.setattr(backup, "_copy_database", fail_copy)
    before = protected_state()
    response = client.post(
        "/mesecni-obracun/", {"leto": 2026, "mesec": 10, "datum_izdaje": "2026-10-08"}
    )
    assert response.status_code == 302
    assert "backup_error=1" in response.url
    assert protected_state() == before
    cycle = MonthlyCycle.objects.get()
    cycle_before_get = MonthlyCycle.objects.values().get(pk=cycle.pk)
    page = client.get(response.url)
    assert page.status_code == 200
    assert (
        "Varnostne kopije ni bilo mogoče" in page.content.decode()
    )
    assert "secret" not in page.content.decode()
    assert MonthlyCycle.objects.values().get(pk=cycle.pk) == cycle_before_get
    assert protected_state() == before
    assert cycle.status == MonthlyCycle.Status.V_TEKU
    cycle.trenutni_korak = MonthlyCycle.Korak.ZAKLJUCEK
    cycle.save()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    before = protected_state()
    response = client.post("/mesecni-obracun/", {"action": "finalize_monthly_cycle"})
    assert response.status_code == 302
    assert "backup_error=1" in response.url
    assert protected_state() == before
    cycle_before_get = MonthlyCycle.objects.values().get(pk=cycle.pk)
    page = client.get(response.url)
    assert page.status_code == 200
    assert "ui/dashboard.html" in [template.name for template in page.templates]
    assert (
        "Varnostne kopije ni bilo mogoče" in page.content.decode()
    )
    assert 'role="alert"' in page.content.decode()
    assert "secret" not in page.content.decode()
    assert MonthlyCycle.objects.values().get(pk=cycle.pk) == cycle_before_get
    assert protected_state() == before
    cycle.refresh_from_db()
    assert cycle.status == MonthlyCycle.Status.ZAKLJUCEN
    assert cycle.completed_at and cycle.completion_snapshot
    assert attempts == [False, True]
    assert BackupSnapshot.objects.count() == 1


def test_backup_warning_does_not_bypass_incomplete_initial_setup(
    locations, monkeypatch, client
):
    from tests.invoicing.test_monthly_cycle import ustvari_podjetje
    from tests.invoicing.test_monthly_cycle_finalization import (
        _cycle,
        _successful,
        _summary,
    )

    company = ustvari_podjetje("Backup company", "12345679")
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)

    def fail_copy(*_):
        raise OSError("secret")

    monkeypatch.setattr(backup, "_copy_database", fail_copy)
    completed = services.zakljuci_mesecni_cikel(cycle)
    assert not completed.backup_result.success
    assert not initial_setup_completed()
    before = MonthlyCycle.objects.values().get(pk=cycle.pk)
    monkeypatch.setattr(
        backup, "create_backup_snapshot", lambda **_: pytest.fail("GET must not back up")
    )
    monkeypatch.setattr(
        "invoicing.backup_hooks.create_backup_snapshot",
        lambda **_: pytest.fail("GET must not back up"),
    )
    response = client.get("/?backup_error=1")
    assert response.status_code == 302
    assert response.url == "/zacetna-nastavitev/"
    for tip in ("NP", "ST"):
        with pytest.raises(ValidationError, match="Začetna nastavitev"):
            dodeli_naslednjo_stevilko(company, tip, date(2026, 10, 8))
    assert MonthlyCycle.objects.values().get(pk=cycle.pk) == before
    assert not InitialSetupCompletion.objects.exists()
    assert not NumberSequence.objects.exists()
    assert not Invoice.objects.exists()
    assert not BackupSnapshot.objects.exists()


def test_rejected_finalization_has_no_post_backup(locations):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 10, 1), datum_izdaje=date(2026, 10, 8)
    )
    with pytest.raises((ValidationError, services.MesecniCikelNapaka)):
        services.zakljuci_mesecni_cikel(cycle)
    assert not BackupSnapshot.objects.exists()


def test_metadata_insert_failure_removes_published_zip(locations, monkeypatch):
    def fail(*args, **kwargs):
        raise ValidationError("database insert failed")

    monkeypatch.setattr(BackupSnapshot, "save", fail)
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert [path.name for path in locations[2].iterdir()] == ["old-backup.zip"]


def test_work_directory_cleanup_failure_cannot_persist_success(locations, monkeypatch):
    original = backup.TemporaryDirectory

    class FailedCleanup(original):
        def __exit__(self, *args):
            super().__exit__(*args)
            raise OSError("cleanup failed")

    monkeypatch.setattr(backup, "TemporaryDirectory", FailedCleanup)
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert [path.name for path in locations[2].iterdir()] == ["old-backup.zip"]


def test_unreadable_archive_directory_has_no_success(locations, monkeypatch):
    original = backup.os.walk

    def denied(root, *args, **kwargs):
        if Path(root) == locations[0]:
            kwargs["onerror"](PermissionError("inaccessible archive"))
        return original(root, *args, **kwargs)

    monkeypatch.setattr(backup.os, "walk", denied)
    assert not create().success
    assert not BackupSnapshot.objects.exists()


def test_reopened_published_zip_database_is_checked(locations, monkeypatch):
    original = backup._verify_zip
    paths = []

    def checked(path, checksum):
        paths.append(Path(path))
        if len(paths) == 2:
            assert Path(path).name.endswith("_backup.zip")
            raise backup.BackupError("Published database checksum differs")
        return original(path, checksum)

    monkeypatch.setattr(backup, "_verify_zip", checked)
    assert not create().success
    assert len(paths) == 2
    assert not BackupSnapshot.objects.exists()
    assert [path.name for path in locations[2].iterdir()] == ["old-backup.zip"]


def test_new_monthly_cycle_cannot_run_backup_inside_transaction(locations):
    from tests.invoicing.test_monthly_cycle import ustvari_podjetje

    ustvari_podjetje("Backup company", "12345679")
    with transaction.atomic(), pytest.raises(ValidationError):
        services.shrani_prvi_korak(date(2026, 10, 1), date(2026, 10, 8))
    assert not MonthlyCycle.objects.exists()
    assert not BackupSnapshot.objects.exists()


def test_concurrent_start_creates_one_pre_monthly_backup(locations):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    from tests.invoicing.test_monthly_cycle import ustvari_podjetje

    ustvari_podjetje("Backup company", "12345679")

    def start():
        close_old_connections()
        try:
            return services.shrani_prvi_korak(date(2026, 10, 1), date(2026, 10, 8)).pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(start) for _ in range(2)]
        identifiers = [future.result(timeout=30) for future in futures]
    assert identifiers[0] == identifiers[1]
    assert MonthlyCycle.objects.count() == 1
    assert BackupSnapshot.objects.count() == 1
    assert BackupSnapshot.objects.get().trigger == BackupSnapshot.Trigger.PRE_MONTHLY


@pytest.mark.parametrize("unavailable", ["missing", "unreadable"])
def test_snapshot_does_not_require_current_icloud_access(
    locations, monkeypatch, unavailable
):
    icloud = locations[1]
    if unavailable == "missing":
        icloud.rename(icloud.with_name("offline-icloud"))
    else:
        original = Path.is_dir

        def denied(path):
            if path == icloud:
                raise PermissionError("iCloud unavailable")
            return original(path)

        monkeypatch.setattr(Path, "is_dir", denied)
    result = create()
    assert result.success, result.message
    with ZipFile(result.snapshot.path) as archive:
        assert archive.read("archive/2026/invoice.pdf") == b"local PDF"
        assert archive.read("media/signature.png") == b"uploaded signature"
        assert not any("duplicate.pdf" in name for name in archive.namelist())


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("media_is_parent", [False, True])
def test_snapshot_rejects_media_overlap_in_both_directions(
    locations, settings, index, media_is_parent
):
    root = locations[index]
    if media_is_parent:
        settings.MEDIA_ROOT = root.parent
    else:
        settings.MEDIA_ROOT = root / "nested-media"
        settings.MEDIA_ROOT.mkdir()
    assert not create().success
    assert not BackupSnapshot.objects.exists()


def test_issued_np_and_st_original_pdfs_survive_offline_restore(
    five_issuers, settings, tmp_path, monkeypatch,
):
    from archive.restore import restore_backup
    from invoicing.services import potrdi_osnutek_st
    from rendering.services import render_pdf
    from tests.companies.test_setup_readiness import issue_np
    from tests.invoicing.test_potrditev_st import draft, review

    data, _ = five_issuers
    complete_initial_setup()
    np = issue_np(data)
    st_draft = draft(data)
    st = potrdi_osnutek_st(st_draft, pregled=review(st_draft))
    originals = [render_pdf(invoice) for invoice in (np, st)]
    assert not DeliveryAttempt.objects.exists()
    assert not ArchivedFile.objects.exists()
    before = [list(model.objects.order_by("pk").values()) for model in (
        Invoice, InvoiceLine, NumberSequence, DeliveryAttempt, ArchivedFile,
    )]
    monkeypatch.setattr("rendering.services._generiraj_pdf_bytes",
        lambda *a: pytest.fail("Never regenerate registered official PDFs"))
    result = create()
    assert result.success, result.message
    roots = resolve_archive_locations()
    target = tmp_path / "offline-recovery"
    restored = restore_backup(result.snapshot.path, target,
        local_root=roots.local_root, icloud_root=roots.icloud_root,
        dropbox_root=roots.dropbox_root)
    assert restored.success, restored.message
    with sqlite3.connect(target / "database/db.sqlite3") as captured:
        for invoice, artifact in zip((np, st), originals, strict=True):
            path, checksum, number = captured.execute(
                "SELECT rendered_pdf_path, checksum, stevilka FROM invoicing_invoice WHERE id=?",
                (invoice.pk,),
            ).fetchone()
            assert (path, checksum, number) == (artifact.pot, artifact.checksum, invoice.stevilka)
            recovered = (target / "rendering" / path).read_bytes()
            assert recovered == artifact.pdf_bytes
            assert hashlib.sha256(recovered).hexdigest() == checksum
    assert [list(model.objects.order_by("pk").values()) for model in (
        Invoice, InvoiceLine, NumberSequence, DeliveryAttempt, ArchivedFile,
    )] == before
