"""Fail-closed archive paths with synthetic saved configuration only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.
import os
from pathlib import Path

import pytest
from django.db import connection

from archive import services
from archive.configuration import ArchiveConfigurationError, operational_archive_roots
from archive.models import ArchiveConfiguration, ArchivedFile
from archive.services import ArchiveRoots, archive_pdf
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from tests.archive.test_services import izdani_racun, roots, sent_invoice

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("side", ["local", "icloud", "both"])
def test_direct_archive_has_no_missing_root_bypass(sent_invoice, roots, side):
    invoice, attempt, _ = sent_invoice
    protected = (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
    )
    missing = (roots.local_root, roots.icloud_root) if side == "both" else (
        getattr(roots, f"{side}_root"),
    )
    for root in missing:
        root.rmdir()

    result = archive_pdf(invoice, roots)

    assert not result.success
    assert not ArchivedFile.objects.exists()
    assert all(not root.exists() for root in missing)
    assert protected == (
        Invoice.objects.values().get(pk=invoice.pk),
        DeliveryAttempt.objects.values().get(pk=attempt.pk),
    )


@pytest.mark.parametrize("fault", [
    "unconfigured", "wrong_roots", "overlap", "database", "database_ancestor",
    "media", "rendering", "dropbox", "file",
])
def test_direct_archive_rejects_unsafe_roots(sent_invoice, roots, settings, tmp_path, monkeypatch, fault):
    invoice, _, _ = sent_invoice
    configuration = ArchiveConfiguration.objects.get()
    passed = roots
    if fault == "unconfigured":
        configuration.delete()
    elif fault == "wrong_roots":
        replacement = roots.local_root.with_name(roots.local_root.name + "-wrong")
        replacement.mkdir()
        passed = ArchiveRoots(replacement, roots.icloud_root)
    elif fault == "overlap":
        nested = roots.local_root / "nested"
        nested.mkdir()
        ArchiveConfiguration.objects.update(icloud_root=str(nested))
        passed = ArchiveRoots(roots.local_root, nested)
    elif fault in {"database", "database_ancestor"}:
        database = roots.local_root / "working.sqlite3" if fault == "database" else roots.local_root.parent
        monkeypatch.setitem(connection.settings_dict, "NAME", str(database))
    elif fault in {"media", "rendering"}:
        setattr(settings, f"{fault.upper()}_ROOT", roots.local_root / "nested")
    elif fault == "dropbox":
        ArchiveConfiguration.objects.update(dropbox_root=str(roots.local_root / "backup"))
    else:
        roots.local_root.rmdir()
        roots.local_root.write_bytes(b"not a directory")

    if fault in {"unconfigured", "overlap", "database", "database_ancestor", "media", "rendering", "dropbox"}:
        with pytest.raises(ArchiveConfigurationError):
            operational_archive_roots()
    assert not archive_pdf(invoice, passed).success
    assert not ArchivedFile.objects.exists()
    assert not list(roots.icloud_root.iterdir())
    if fault == "file":
        assert roots.local_root.read_bytes() == b"not a directory"


@pytest.mark.parametrize("side", ["local", "icloud"])
def test_root_disappears_after_validation_without_recreation(sent_invoice, roots, monkeypatch, side):
    invoice, _, source = sent_invoice
    missing = getattr(roots, f"{side}_root")
    original = services._publish

    def disappear(path, *args):
        if path.is_relative_to(missing):
            missing.rmdir()
        return original(path, *args)

    with monkeypatch.context() as scoped:
        scoped.setattr(services, "_publish", disappear)
        first = archive_pdf(invoice, roots)

    assert not first.success and not missing.exists()
    record = ArchivedFile.objects.get()
    assert record.status == ArchivedFile.Status.IN_PROGRESS
    good = Path(record.icloud_path if side == "local" else record.local_path)
    assert good.read_bytes() == source
    mtime = good.stat().st_mtime_ns
    missing.mkdir()  # Explicit restoration by the operator, never by archive_pdf.
    retry = archive_pdf(invoice, roots)
    assert retry.success and retry.archived_file_id == record.pk
    assert good.stat().st_mtime_ns == mtime
    assert Path(retry.local_path).read_bytes() == Path(retry.icloud_path).read_bytes() == source


def test_root_disappears_during_first_child_creation(sent_invoice, roots, monkeypatch):
    invoice, _, _ = sent_invoice
    original_mkdir = os.mkdir
    missing = roots.icloud_root

    def disappear(path, *args, **kwargs):
        if (kwargs.get("dir_fd") is not None and path == "Arhiv") or Path(path) == missing / "Arhiv":
            # POSIX reaches local first. Remove iCloud only on its own descriptor.
            if kwargs.get("dir_fd") is not None:
                opened = os.fstat(kwargs["dir_fd"])
                current = missing.stat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    return original_mkdir(path, *args, **kwargs)
            missing.rmdir()
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", disappear)
    assert not archive_pdf(invoice, roots).success
    assert not missing.exists()
    assert ArchivedFile.objects.get().status == ArchivedFile.Status.IN_PROGRESS


@pytest.mark.parametrize("location", ["root", "descendant"])
def test_symlink_escape_writes_nothing(sent_invoice, roots, tmp_path, location):
    invoice, _, _ = sent_invoice
    outside = tmp_path / "outside"
    outside.mkdir()
    link = roots.local_root if location == "root" else roots.local_root / "Arhiv"
    if location == "root":
        link.rmdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require OS privileges")
    assert not archive_pdf(invoice, roots).success
    assert not ArchivedFile.objects.exists()
    assert not list(outside.iterdir())
