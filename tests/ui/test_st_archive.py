"""Operational ST archive/backup controls, with isolated storage only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest
from django.db import connection, models
from django.test import Client
from django.urls import reverse

from archive.models import ArchiveConfiguration, ArchivedFile, BackupSnapshot
from invoicing.models import Invoice
from tests.archive.test_st_archive import (
    issued_invoice,
    protected_data,
    sent_st,
    st_invoice,
)

pytestmark = pytest.mark.django_db(transaction=True)


def url(name, invoice, **changes):
    return reverse(f"ui:st_{name}", kwargs={
        "company_id": invoice.company_id, "invoice_id": invoice.pk, **changes,
    })


def test_detail_get_is_read_only_and_archive_post_is_explicit(client, sent_st, monkeypatch):
    from archive import services

    before = protected_data()
    with monkeypatch.context() as patch:
        patch.setattr(services, "archive_pdf", lambda *a, **k: pytest.fail("GET must not archive"))
        page = client.get(url("detail", sent_st))
    assert page.status_code == 200
    assert "Račun je pripravljen za arhiviranje." in page.content.decode()
    assert "Arhiviraj ST račun" in page.content.decode()
    assert not ArchivedFile.objects.exists()
    assert client.get(url("archive", sent_st)).status_code == 405
    response = client.post(url("archive", sent_st))
    assert response.status_code == 302
    content = client.get(response.url).content.decode()
    assert "Obe arhivski kopiji sta prisotni in preverjeni" in content
    assert "Varnostna kopija je ločeno dejanje" in content
    assert reverse("ui:nastavitve_arhiv") + "#varnostne-kopije" in content
    assert not BackupSnapshot.objects.exists()
    assert protected_data() == before


def test_live_get_reports_missing_corrupt_and_conflicting_copies_without_writes(client, sent_st):
    assert client.post(url("archive", sent_st)).status_code == 302
    record = ArchivedFile.objects.get()
    local, icloud = Path(record.local_path), Path(record.icloud_path)
    saved = local.read_bytes()
    before = list(ArchivedFile.objects.values())
    local.unlink()
    page = client.get(url("detail", sent_st))
    assert "Lokalna arhivska kopija manjka" in page.content.decode()
    assert "Ponovi nepopolno arhiviranje" in page.content.decode()
    assert not local.exists()
    assert list(ArchivedFile.objects.values()) == before
    local.write_bytes(saved)
    icloud.write_bytes(b"corrupt")
    assert "Kontrolna vsota kopije iCloud se ne ujema" in client.get(url("detail", sent_st)).content.decode()
    assert icloud.read_bytes() == b"corrupt"
    ArchivedFile.objects.update(checksum="0" * 64)
    assert "Konflikt metapodatkov" in client.get(url("detail", sent_st)).content.decode()


def test_unsent_detail_has_no_archive_control(client, sent_st):
    models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk), status="IZBRAN_ZA_POSILJANJE")
    page = client.get(url("detail", sent_st))
    assert "Račun še ni upravičen do arhiviranja" in page.content.decode()
    assert "Arhiviraj ST račun" not in page.content.decode()
    assert client.post(url("archive", sent_st)).status_code == 409
    assert not ArchivedFile.objects.exists()


def test_csrf_company_scope_and_invoice_type_protect_post(sent_st):
    assert Client(enforce_csrf_checks=True).post(url("archive", sent_st)).status_code == 403
    client = Client()
    assert client.post(url("archive", sent_st, company_id=sent_st.company_id + 1)).status_code == 404
    models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk), document_type="NP")
    assert client.post(url("archive", sent_st)).status_code == 404
    assert not ArchivedFile.objects.exists()


def test_partial_failure_shows_safe_feedback_and_retry_never_resends(client, sent_st, monkeypatch):
    from archive import services

    real = services._publish
    failed_root = Path(ArchiveConfiguration.objects.get().icloud_root)

    def fail(path, *args):
        if path.is_relative_to(failed_root):
            raise OSError("SECRET C:/Users/private/sqlite/secret.sqlite3")
        return real(path, *args)

    with monkeypatch.context() as patch:
        patch.setattr(services, "_publish", fail)
        page = client.post(url("archive", sent_st))
    assert page.status_code == 409
    content = page.content.decode()
    assert "Kopija v arhivu iCloud manjka" in content
    assert "Ponovi nepopolno arhiviranje" in content
    assert "SECRET" not in content and "C:/Users/private" not in content
    before = protected_data()
    assert client.post(url("archive", sent_st)).status_code == 302
    assert protected_data() == before


def test_atomic_requests_excludes_archive_live_get_and_manual_backup(client, sent_st, monkeypatch):
    from archive import backup, services

    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    original_hash, original_copy = services._hash_file, backup._copy_database

    def hash_outside_transaction(*args):
        assert not connection.in_atomic_block
        return original_hash(*args)

    def copy_outside_transaction(*args):
        assert not connection.in_atomic_block
        return original_copy(*args)

    monkeypatch.setattr(services, "_hash_file", hash_outside_transaction)
    monkeypatch.setattr(backup, "_copy_database", copy_outside_transaction)
    assert client.post(url("archive", sent_st)).status_code == 302
    assert client.get(url("detail", sent_st)).context["archive_state"].verified
    assert client.post(reverse("ui:nastavitve_arhiv"), {"action": "create_backup"}).url.endswith("backup=success")


def test_manual_backup_after_archive_contains_verified_st_database_and_media(client, sent_st, settings, tmp_path):
    from archive.restore import restore_backup

    attachment = settings.MEDIA_ROOT / "attachment.pdf"
    attachment.write_bytes(b"uploaded attachment must survive")
    (settings.MEDIA_ROOT / "logo.png").write_bytes(b"synthetic uploaded logo")
    assert client.post(url("archive", sent_st)).status_code == 302
    record = ArchivedFile.objects.get()
    archive_before = list(ArchivedFile.objects.values())
    before = protected_data()
    response = client.post(reverse("ui:nastavitve_arhiv"), {"action": "create_backup"})
    assert response.url.endswith("backup=success")
    snapshot = BackupSnapshot.objects.get()
    assert snapshot.trigger == "MANUAL"
    assert snapshot.created_at >= record.created_at
    path = Path(snapshot.path)
    assert sha256(path.read_bytes()).hexdigest() == snapshot.zip_checksum
    config = ArchiveConfiguration.objects.get()
    relative = Path(record.local_path).relative_to(config.local_root).as_posix()
    with ZipFile(path) as zipped:
        assert zipped.testzip() is None
        manifest = json.loads(zipped.read("manifest.json"))
        database_bytes = zipped.read("database/db.sqlite3")
        assert manifest["trigger"] == "MANUAL"
        assert sha256(database_bytes).hexdigest() == manifest["database_checksum"] == snapshot.database_checksum
        assert zipped.read("archive/" + relative) == Path(record.local_path).read_bytes()
        assert zipped.read("media/attachment.pdf") == attachment.read_bytes()
    copied = tmp_path / "inspection.sqlite3"
    copied.write_bytes(database_bytes)
    with sqlite3.connect(copied) as database:
        assert database.execute("SELECT status, checksum FROM archive_archivedfile WHERE invoice_id=?", (sent_st.pk,)).fetchone() == ("VERIFIED", sent_st.checksum)
    restored = restore_backup(path, tmp_path / "recovery", local_root=config.local_root,
        icloud_root=config.icloud_root, dropbox_root=config.dropbox_root)
    assert restored.success, restored.message
    assert (restored.target_directory / "archive" / relative).read_bytes() == Path(record.local_path).read_bytes()
    assert (restored.target_directory / "media/attachment.pdf").read_bytes() == attachment.read_bytes()
    assert list(ArchivedFile.objects.values()) == archive_before
    assert protected_data()[:-1] == before[:-1]


def test_backup_failure_preserves_verified_archive_and_delivery(client, sent_st, monkeypatch):
    from archive import backup

    assert client.post(url("archive", sent_st)).status_code == 302
    before, archive_before = protected_data(), list(ArchivedFile.objects.values())

    def fail(*args):
        raise OSError("private backup path")

    monkeypatch.setattr(backup, "_copy_database", fail)
    response = client.post(reverse("ui:nastavitve_arhiv"), {"action": "create_backup"})
    assert response.url.endswith("backup=failed")
    page = client.get(response.url)
    assert "private backup path" not in page.content.decode()
    assert "Varnostne kopije ni bilo mogoče ustvariti" in page.content.decode()
    assert protected_data() == before
    assert list(ArchivedFile.objects.values()) == archive_before
    assert client.get(url("detail", sent_st)).context["archive_state"].verified
