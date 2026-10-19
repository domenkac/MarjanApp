"""Snapshot PDF inventory and offline recovery; synthetic isolated data only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

import hashlib
import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import pytest
from django.db import connection, models

from archive import backup
from archive.models import ArchivedFile, BackupSnapshot
from companies.models import Company, NumberSequence
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice, InvoiceLine
from tests.archive.test_backup import create, locations
from tests.archive.test_restore import alter_database, recover, rewrite
from tests.invoicing.test_invoice_models import podatki_racuna

pytestmark = pytest.mark.django_db(transaction=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def official_pdfs(locations, settings):
    company = Company.objects.create(naziv="Synthetic issuer", davcna_stevilka="15012557")
    invoices = []
    for kind, number in (("NP", "NP-00001"), ("ST", "ST-2026-0001")):
        data = b"%PDF-1.7\nSynthetic original " + kind.encode() + b"\n%%EOF"
        relative = f"company-{company.pk}/{number}.pdf"
        path = settings.RENDERING_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        invoices.append(Invoice.objects.create(**podatki_racuna(
            company, document_type=kind, stevilka=number,
            status="IZBRAN_ZA_POSILJANJE", rendered_pdf_path=relative,
            checksum=sha(data),
        )))
    # None of these unrelated files belongs in the rendering inventory.
    for name in (".render-private.tmp", "secret.key", "VZOREC-test.pdf", ".pdf.lock"):
        (settings.RENDERING_ROOT / name).write_bytes(b"exclude")
    return invoices


def protected_rows():
    return [list(model.objects.order_by("pk").values()) for model in (
        Invoice, InvoiceLine, NumberSequence, DeliveryAttempt, ArchivedFile,
    )]


@pytest.mark.parametrize("state", ["pending", "failure", "unknown", "archived", "partial"])
def test_original_np_and_st_round_trip(official_pdfs, locations, settings, tmp_path, state):
    for invoice in official_pdfs:
        data = (settings.RENDERING_ROOT / invoice.rendered_pdf_path).read_bytes()
        if state != "pending":
            result, status = {
                "failure": ("CONFIRMED_FAILURE", "NAPAKA"),
                "unknown": ("UNKNOWN", "REZULTAT_NI_ZNAN"),
                "archived": ("ACCEPTED", "POSLAN"),
                "partial": ("ACCEPTED", "POSLAN"),
            }[state]
            models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), status=status)
            DeliveryAttempt.objects.create(
                invoice=invoice, sequence=1, result=result, sender="sender@example.com",
                recipients_to=["recipient@example.com"],
                pdf_path=invoice.rendered_pdf_path, pdf_checksum=invoice.checksum,
            )
        if state in {"archived", "partial"}:
            relative = invoice.rendered_pdf_path
            local, cloud = (root / relative for root in locations[:2])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            if state == "archived":
                cloud.parent.mkdir(parents=True, exist_ok=True)
                cloud.write_bytes(data)
            ArchivedFile.objects.create(
                invoice=invoice, artifact_type="PDF", local_path=str(local),
                icloud_path=str(cloud), checksum=invoice.checksum,
                status="VERIFIED" if state == "archived" else "IN_PROGRESS",
            )
    before = protected_rows()
    original = {i.rendered_pdf_path: (settings.RENDERING_ROOT / i.rendered_pdf_path).read_bytes()
                for i in official_pdfs}
    result = create()
    assert result.success, result.message
    source = Path(result.snapshot.path)
    with ZipFile(source) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 2
        assert manifest["official_pdfs"] == [
            {"invoice_id": i.pk, "path": i.rendered_pdf_path, "checksum": i.checksum}
            for i in official_pdfs
        ]
        assert {name for name in archive.namelist()
                if name.startswith("rendering/") and not name.endswith("/")} == {
                    f"rendering/{name}" for name in original
                }
        for name, data in original.items():
            assert archive.read(f"rendering/{name}") == data
    target = tmp_path / "recovery"
    with connection.execute_wrapper(lambda *a: pytest.fail("No live SQL during restore")):
        recovered = recover((source, locations), target)
    assert recovered.success, recovered.message
    with sqlite3.connect(target / "database/db.sqlite3") as captured:
        for path, checksum in captured.execute(
            "SELECT rendered_pdf_path, checksum FROM invoicing_invoice"
        ):
            data = (target / "rendering" / path).read_bytes()
            assert data == original[path]
            assert sha(data) == checksum
    assert protected_rows() == before
    assert all((settings.RENDERING_ROOT / name).read_bytes() == data
               for name, data in original.items())


@pytest.mark.parametrize("damage", [
    "missing", "corrupt", "path_only", "checksum_only", "bad_hash", "traversal",
    "absolute", "wrong_company", "wrong_number", "unsafe_number",
])
def test_invalid_registered_pdf_preserves_previous_backup(
    official_pdfs, locations, settings, damage,
):
    first = create()
    assert first.success
    previous = Path(first.snapshot.path).read_bytes()
    invoice = official_pdfs[0]
    path = settings.RENDERING_ROOT / invoice.rendered_pdf_path
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        changes = {
            "path_only": {"checksum": ""},
            "checksum_only": {"rendered_pdf_path": ""},
            "bad_hash": {"checksum": "bad"},
            "traversal": {"rendered_pdf_path": "../secret.pdf"},
            "absolute": {"rendered_pdf_path": str(path)},
            "wrong_company": {"rendered_pdf_path": "company-999/NP-00001.pdf"},
            "wrong_number": {"rendered_pdf_path": f"company-{invoice.company_id}/NP-00002.pdf"},
            "unsafe_number": {"stevilka": "../secret"},
        }[damage]
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), **changes)
    before = protected_rows()
    assert not create().success
    assert BackupSnapshot.objects.count() == 1
    assert Path(first.snapshot.path).read_bytes() == previous
    assert sorted(p.name for p in locations[2].iterdir()) == sorted([
        "old-backup.zip", Path(first.snapshot.path).name,
    ])
    assert protected_rows() == before


@pytest.mark.parametrize("directory", [False, True])
def test_render_symlink_escape_is_rejected(official_pdfs, settings, tmp_path, directory):
    invoice = official_pdfs[0]
    path = settings.RENDERING_ROOT / invoice.rendered_pdf_path
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / path.name).write_bytes(path.read_bytes())
    if directory:
        for child in path.parent.iterdir():
            child.unlink()
        path.parent.rmdir()
        link, destination = path.parent, outside
    else:
        path.unlink()
        link, destination = path, outside / path.name
    try:
        link.symlink_to(destination, target_is_directory=directory)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert (outside / path.name).exists()


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("direction", ["inside", "parent", "same"])
def test_render_root_must_not_overlap_other_roots(locations, settings, index, direction):
    root = locations[index]
    settings.RENDERING_ROOT = {
        "inside": root / "rendered", "parent": root.parent, "same": root,
    }[direction]
    assert not create().success
    assert not BackupSnapshot.objects.exists()


def test_database_must_not_be_inside_render_root(locations, settings):
    settings.RENDERING_ROOT = Path(connection.settings_dict["NAME"]).parent
    assert not create().success


def test_inventory_uses_captured_database_not_later_registrations(
    official_pdfs, settings, monkeypatch,
):
    original = backup._copy_database
    late = Invoice.objects.create(**podatki_racuna(official_pdfs[0].company, stevilka="NP-00002"))

    def capture_then_publish(target):
        original(target)
        relative = f"company-{late.company_id}/{late.stevilka}.pdf"
        (settings.RENDERING_ROOT / relative).write_bytes(b"later publication")
        models.QuerySet.update(Invoice.objects.filter(pk=late.pk),
            rendered_pdf_path=relative, checksum=sha(b"later publication"))

    monkeypatch.setattr(backup, "_copy_database", capture_then_publish)
    result = create()
    assert result.success
    with ZipFile(result.snapshot.path) as archive:
        inventory = json.loads(archive.read("manifest.json"))["official_pdfs"]
        assert [item["invoice_id"] for item in inventory] == [i.pk for i in official_pdfs]
        assert not any("NP-00002" in name for name in archive.namelist())


def test_pdf_changed_during_copy_fails_closed(official_pdfs, settings, monkeypatch):
    path = settings.RENDERING_ROOT / official_pdfs[0].rendered_pdf_path
    original = backup.shutil.copyfileobj

    def copy_then_damage(source, destination, **kwargs):
        original(source, destination, **kwargs)
        path.write_bytes(b"changed during backup")

    original_add = backup._add_pdf

    def damage_during_copy(output, root, artifact):
        assert not connection.in_atomic_block and connection.get_autocommit()
        with monkeypatch.context() as patch:
            patch.setattr(backup.shutil, "copyfileobj", copy_then_damage)
            return original_add(output, root, artifact)

    monkeypatch.setattr(backup, "_add_pdf", damage_during_copy)
    assert not create().success
    assert not BackupSnapshot.objects.exists()


@pytest.mark.parametrize("damage", ["missing", "bytes", "inventory", "extra", "downgrade"])
def test_restore_rejects_incomplete_or_forged_v2(
    official_pdfs, locations, tmp_path, damage,
):
    result = create()
    assert result.success
    snapshot = (Path(result.snapshot.path), locations)
    entry = "rendering/" + official_pdfs[0].rendered_pdf_path

    def replace(name, data):
        if damage == "bytes" and name == entry:
            return b"forged PDF"
        if name == "manifest.json" and damage in {"inventory", "downgrade"}:
            manifest = json.loads(data)
            if damage == "inventory":
                manifest["official_pdfs"] = []
            else:
                manifest["format_version"] = 1
            return json.dumps(manifest).encode()
        return data

    source = rewrite(snapshot, tmp_path, replace=replace,
        omit=(entry,) if damage == "missing" else (),
        extra=[("rendering/secret.key", b"exclude")] if damage == "extra" else ())
    target = tmp_path / "recovery"
    assert not recover(snapshot, target, source).success
    assert not target.exists()
    assert not list(tmp_path.glob(".marjanapp-restore-*"))


def test_legacy_restore_is_explicitly_incomplete(official_pdfs, locations, tmp_path):
    result = create()
    assert result.success
    snapshot = (Path(result.snapshot.path), locations)

    def legacy_manifest(name, data):
        if name == "manifest.json":
            manifest = json.loads(data)
            manifest["format_version"] = 1
            del manifest["official_pdfs"]
            del manifest["rendering_root"]
            return json.dumps(manifest).encode()
        return data

    with ZipFile(snapshot[0]) as archive:
        omit = [name for name in archive.namelist() if name.startswith("rendering/")]
    source = rewrite(snapshot, tmp_path, replace=legacy_manifest, omit=omit)
    target = tmp_path / "legacy-recovery"
    recovered = recover(snapshot, target, source)
    assert recovered.success, recovered.message
    assert "Legacy v1" in recovered.message
    assert "official PDF completeness is not verified" in recovered.message
    assert not (target / "rendering").exists()
    with sqlite3.connect(target / "database/db.sqlite3") as captured:
        assert captured.execute("SELECT checksum FROM invoicing_invoice").fetchall() == [
            (invoice.checksum,) for invoice in official_pdfs
        ]


def test_same_number_in_different_issuers_has_distinct_bytes(official_pdfs, settings, tmp_path):
    first = official_pdfs[0]
    company = Company.objects.create(naziv="Second issuer", davcna_stevilka="20000006")
    relative = f"company-{company.pk}/{first.stevilka}.pdf"
    path = settings.RENDERING_ROOT / relative
    path.parent.mkdir()
    data = b"%PDF-1.7\nSecond issuer original\n%%EOF"
    path.write_bytes(data)
    Invoice.objects.create(**podatki_racuna(company, stevilka=first.stevilka,
        rendered_pdf_path=relative, checksum=sha(data), status="IZBRAN_ZA_POSILJANJE"))
    results = [create(), create()]
    assert all(result.success for result in results)
    assert results[0].snapshot.path != results[1].snapshot.path
    for result in results:
        with ZipFile(result.snapshot.path) as archive:
            assert archive.read(f"rendering/{relative}") == data
            assert archive.read(f"rendering/{first.rendered_pdf_path}") != data


def test_inventory_does_not_follow_live_metadata_changes(official_pdfs, monkeypatch):
    original = backup._copy_database

    def capture_then_change(target):
        original(target)
        models.QuerySet.update(Invoice.objects.filter(pk=official_pdfs[0].pk),
                              rendered_pdf_path="../invalid-live-path.pdf")

    monkeypatch.setattr(backup, "_copy_database", capture_then_change)
    result = create()
    assert result.success
    with ZipFile(result.snapshot.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["official_pdfs"][0]["path"] == official_pdfs[0].rendered_pdf_path


def test_forged_snapshot_metadata_rejected(official_pdfs, locations, tmp_path):
    result = create()
    snapshot = (Path(result.snapshot.path), locations)
    source = alter_database(snapshot, tmp_path,
        "UPDATE invoicing_invoice SET rendered_pdf_path='../secret.pdf'")
    target = tmp_path / "recovery"
    assert not recover(snapshot, target, source).success
    assert not target.exists()


def test_extracted_pdf_is_verified(official_pdfs, locations, tmp_path, monkeypatch):
    result = create()
    snapshot = (Path(result.snapshot.path), locations)
    original = backup.shutil.copyfileobj

    def damage_extraction(incoming, outgoing, **kwargs):
        original(incoming, outgoing, **kwargs)
        name = getattr(outgoing, "name", "")
        if isinstance(name, str) and Path(name).suffix == ".pdf":
            outgoing.write(b"corruption after extraction")

    monkeypatch.setattr(backup.shutil, "copyfileobj", damage_extraction)
    target = tmp_path / "recovery"
    assert not recover(snapshot, target).success
    assert not target.exists()
    assert not list(tmp_path.glob(".marjanapp-restore-*"))


@pytest.mark.parametrize("historical", [False, True])
def test_restore_protects_render_root(
    official_pdfs, locations, settings, tmp_path, historical,
):
    result = create()
    snapshot = (Path(result.snapshot.path), locations)
    target = settings.RENDERING_ROOT / "recovery"
    if historical:
        settings.RENDERING_ROOT = tmp_path / "new-render-root"
    assert not recover(snapshot, target).success
    assert not target.exists()


@pytest.mark.parametrize("unavailable", ["missing", "unreadable"])
def test_official_pdfs_do_not_require_icloud(official_pdfs, locations, monkeypatch, unavailable):
    icloud = locations[1]
    if unavailable == "missing":
        icloud.rename(icloud.with_name("offline-icloud"))
    else:
        original = Path.is_dir

        def denied(path):
            if path == icloud:
                raise PermissionError("Cloud unavailable")
            return original(path)

        monkeypatch.setattr(Path, "is_dir", denied)
    result = create()
    assert result.success
    with ZipFile(result.snapshot.path) as archive:
        for invoice in official_pdfs:
            assert sha(archive.read(f"rendering/{invoice.rendered_pdf_path}")) == invoice.checksum


@pytest.mark.parametrize("index", range(4))
def test_scratch_cannot_be_inside_a_backup_root(locations, monkeypatch, index):
    monkeypatch.setattr(backup, "gettempdir", lambda: str(locations[index]))
    assert not create().success
    assert not BackupSnapshot.objects.exists()


@pytest.mark.parametrize("index", [0, 3])
def test_live_database_hardlink_is_not_copied(locations, index):
    path = locations[index] / "disguised.sqlite3"
    path.hardlink_to(Path(connection.settings_dict["NAME"]))
    assert not create().success
    assert not BackupSnapshot.objects.exists()
    assert not list(locations[2].glob(".marjanapp-backup-*"))
