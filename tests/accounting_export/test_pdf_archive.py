# ruff: noqa: F401, F811
import hashlib
from datetime import date
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from accounting_export import archive
from accounting_export.archive import (
    PdfArchiveState,
    archive_accountant_report_pdf,
    verify_accountant_report_pdf_artifact,
)
from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from archive.models import ArchivedFile
from companies.models import Company
from tests.accounting_export.test_csv_renderer import report

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def persisted_report(report):
    company = Company.objects.create(naziv="Live name", davcna_stevilka="15012557")
    return AccountantReport.objects.create(
        company=company, period=date(2026, 7, 1), generated_at=timezone.now(),
        schema_version=1, snapshot=report.snapshot,
    )


@pytest.fixture
def root(tmp_path):
    return tmp_path / "local"


@pytest.fixture
def pdf(monkeypatch):
    def render(report):
        assert not connection.in_atomic_block
        assert report.snapshot["header"]["company_name"] == "Živa Alfa d.o.o."
        return b"%PDF-test snapshot"
    monkeypatch.setattr(archive, "render_accountant_report_pdf", render)
    return b"%PDF-test snapshot"


def test_local_verified_idempotent(persisted_report, root, pdf, monkeypatch):
    before = AccountantReport.objects.values().get(pk=persisted_report.pk)
    # Caller edits must never replace persisted historical data.
    persisted_report.snapshot["header"]["company_name"] = "Caller mutation"
    first = archive_accountant_report_pdf(persisted_report, root)
    assert first.state == PdfArchiveState.VERIFIED, first.message
    artifact = AccountantReportPdfArtifact.objects.get()
    path = Path(artifact.local_path)
    assert path.is_relative_to(root)
    assert "2026/07" in path.as_posix()
    assert f"report-{persisted_report.pk}" in path.parts
    assert path.read_bytes() == pdf
    assert artifact.checksum == hashlib.sha256(pdf).hexdigest()
    timestamp = path.stat().st_mtime_ns
    monkeypatch.setattr(archive, "render_accountant_report_pdf", lambda _: pytest.fail("rerender"))
    assert archive_accountant_report_pdf(persisted_report, root).success
    assert path.stat().st_mtime_ns == timestamp
    assert AccountantReportPdfArtifact.objects.count() == 1
    assert list(root.parent.iterdir()) == [root]
    assert not ArchivedFile.objects.exists()
    assert AccountantReport.objects.values().get(pk=persisted_report.pk) == before


@pytest.mark.parametrize("same", [True, False])
def test_existing_file_never_overwritten(persisted_report, root, pdf, same):
    path = root / archive.accountant_report_pdf_relative_path(persisted_report)
    path.parent.mkdir(parents=True)
    existing = pdf if same else b"conflict"
    path.write_bytes(existing)
    result = archive_accountant_report_pdf(persisted_report, root)
    assert result.success == same
    assert path.read_bytes() == existing
    assert AccountantReportPdfArtifact.objects.count() == int(same)


@pytest.mark.parametrize("state", ["MISSING", "CHECKSUM_MISMATCH", "METADATA_CONFLICT", "FAILED"])
def test_verification_failures(persisted_report, root, pdf, state, monkeypatch):
    assert archive_accountant_report_pdf(persisted_report, root).success
    artifact = AccountantReportPdfArtifact.objects.get()
    path = Path(artifact.local_path)
    if state == "MISSING":
        path.unlink()
    elif state == "CHECKSUM_MISMATCH":
        path.write_bytes(b"corrupt")
    elif state == "METADATA_CONFLICT":
        artifact.local_path = str(root.parent / "outside.pdf")
    else:
        monkeypatch.setattr(archive, "_hash_file", lambda _: (_ for _ in ()).throw(PermissionError()))
    result = verify_accountant_report_pdf_artifact(artifact, root)
    assert result.state == state
    if state in {"MISSING", "CHECKSUM_MISMATCH"}:
        assert archive_accountant_report_pdf(persisted_report, root).state == state


@pytest.mark.parametrize("value", ["../escape", "C:\\escape", "a/b", "a\\b", "..", "a:stream", "CON", "a\x00"])
def test_unsafe_history_rejected(persisted_report, root, pdf, value):
    # Construct the historical value before creation, preserving report immutability.
    snapshot = persisted_report.snapshot
    snapshot["header"]["company_name"] = value
    report = AccountantReport.objects.create(
        company_id=persisted_report.company_id, period=persisted_report.period,
        generated_at=persisted_report.generated_at, schema_version=1, snapshot=snapshot,
    )
    assert archive_accountant_report_pdf(report, root).state == "METADATA_CONFLICT"
    assert not root.exists()


def test_root_persistence_and_transaction_requirements(persisted_report, root, pdf, report):
    assert not archive_accountant_report_pdf(report, root).success
    assert not archive_accountant_report_pdf(persisted_report, Path("relative")).success
    with transaction.atomic():
        assert not archive_accountant_report_pdf(persisted_report, root).success
    assert not root.exists()


def test_distinct_snapshot_paths(persisted_report, root, pdf):
    second = AccountantReport.objects.create(
        company_id=persisted_report.company_id, period=persisted_report.period,
        generated_at=persisted_report.generated_at, schema_version=1,
        snapshot=persisted_report.snapshot,
    )
    one = archive_accountant_report_pdf(persisted_report, root)
    two = archive_accountant_report_pdf(second, root)
    assert one.success and two.success
    assert one.local_path != two.local_path


def test_metadata_only_after_verified_write_and_safe_db_retry(persisted_report, root, pdf, monkeypatch):
    manager = AccountantReportPdfArtifact.objects
    original = manager.get_or_create
    def fail(**kwargs):
        assert Path(kwargs["defaults"]["local_path"]).read_bytes() == pdf
        assert connection.in_atomic_block
        raise IntegrityError("DB unavailable")
    monkeypatch.setattr(manager, "get_or_create", fail)
    with pytest.raises(IntegrityError):
        archive_accountant_report_pdf(persisted_report, root)
    assert manager.count() == 0
    monkeypatch.setattr(manager, "get_or_create", original)
    assert archive_accountant_report_pdf(persisted_report, root).success


def test_failed_publication_has_no_metadata(persisted_report, root, pdf, monkeypatch):
    monkeypatch.setattr(archive, "_publish", lambda *args: (_ for _ in ()).throw(PermissionError()))
    assert archive_accountant_report_pdf(persisted_report, root).state == "FAILED"
    assert not AccountantReportPdfArtifact.objects.exists()


def test_immutable_metadata_and_protection(persisted_report, root, pdf):
    assert archive_accountant_report_pdf(persisted_report, root).success
    artifact = AccountantReportPdfArtifact.objects.get()
    with pytest.raises(ValidationError):
        artifact.save()
    with pytest.raises(ValidationError):
        artifact.delete()
    with pytest.raises(ValidationError):
        AccountantReportPdfArtifact.objects.filter(pk=artifact.pk).update(checksum="a" * 64)
    with pytest.raises(ValidationError):
        AccountantReportPdfArtifact.objects.all().delete()
    with pytest.raises(ValidationError):
        persisted_report.save()
    with pytest.raises(ProtectedError):
        AccountantReport.objects.filter(pk=persisted_report.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountantReportPdfArtifact.objects.create(
            report=persisted_report, local_path=artifact.local_path,
            checksum=artifact.checksum, verified_at=artifact.verified_at,
        )


def test_archive_queries_only_report_and_artifact(persisted_report, root, pdf, django_assert_num_queries):
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        assert archive_accountant_report_pdf(persisted_report, root).success
    sql = " ".join(query["sql"] for query in queries.captured_queries).lower()
    for table in ("companies_company", "invoicing_invoice", "contracts_lease", "catalogs_vatregime"):
        assert table not in sql


def test_real_pdf_archive(persisted_report, root):
    result = archive_accountant_report_pdf(persisted_report, root)
    assert result.success, result.message
    assert Path(result.local_path).read_bytes().startswith(b"%PDF-")


def test_retry_with_different_rendered_bytes_fails(persisted_report, root, pdf, monkeypatch):
    manager = AccountantReportPdfArtifact.objects
    original = manager.get_or_create
    monkeypatch.setattr(manager, "get_or_create", lambda **kwargs: (_ for _ in ()).throw(IntegrityError()))
    with pytest.raises(IntegrityError):
        archive_accountant_report_pdf(persisted_report, root)
    monkeypatch.setattr(manager, "get_or_create", original)
    monkeypatch.setattr(archive, "render_accountant_report_pdf", lambda _: b"%PDF-different")
    result = archive_accountant_report_pdf(persisted_report, root)
    assert result.state == "METADATA_CONFLICT"
    assert Path(result.local_path).read_bytes() == pdf
    assert not manager.exists()


def test_final_hash_must_match_before_metadata(persisted_report, root, pdf, monkeypatch):
    original = archive._publish
    def corrupt(path, data, checksum):
        original(path, data, checksum)
        path.write_bytes(b"corrupted after publication")
    monkeypatch.setattr(archive, "_publish", corrupt)
    assert archive_accountant_report_pdf(persisted_report, root).state == "METADATA_CONFLICT"
    assert not AccountantReportPdfArtifact.objects.exists()


def test_hashing_outside_transaction(persisted_report, root, pdf, monkeypatch):
    original = archive._hash_file
    def checked(path):
        assert not connection.in_atomic_block
        return original(path)
    monkeypatch.setattr(archive, "_hash_file", checked)
    assert archive_accountant_report_pdf(persisted_report, root).success


def test_root_change_is_metadata_conflict(persisted_report, root, pdf):
    assert archive_accountant_report_pdf(persisted_report, root).success
    other = root.parent / "other"
    assert archive_accountant_report_pdf(persisted_report, other).state == "METADATA_CONFLICT"
    assert not other.exists()


def test_bulk_overwrite_is_rejected(persisted_report, root, pdf):
    assert archive_accountant_report_pdf(persisted_report, root).success
    artifact = AccountantReportPdfArtifact.objects.get()
    artifact.checksum = "a" * 64
    with pytest.raises(ValidationError):
        AccountantReportPdfArtifact.objects.bulk_update([artifact], ["checksum"])
    with pytest.raises(ValidationError):
        AccountantReportPdfArtifact.objects.bulk_create(
            [artifact], update_conflicts=True, update_fields=["checksum"], unique_fields=["report"],
        )


def test_symlink_escape_rejected(persisted_report, root, pdf):
    root.mkdir()
    outside = root.parent / "outside"
    outside.mkdir()
    try:
        (root / "Kontrolni-dokumenti").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    assert archive_accountant_report_pdf(persisted_report, root).state == "METADATA_CONFLICT"
    assert not list(outside.iterdir())
