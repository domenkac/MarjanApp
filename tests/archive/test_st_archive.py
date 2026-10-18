"""ST archival uses temporary files and the test SQLite database only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, models, transaction
from django.utils import timezone

from archive.models import ArchiveConfiguration, ArchivedFile, BackupSnapshot
from archive.st_invoices import archive_st_invoice, st_archive_status
from companies.models import NumberSequence
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from delivery.services import resolve_unknown_as_sent
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.delivery.test_delivery_services import issued_invoice
from tests.delivery.test_st_delivery import st_invoice

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sent_st(st_invoice, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_ROOT.mkdir()
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    for root in roots:
        root.mkdir()
    ArchiveConfiguration.objects.create(
        local_root=str(roots[0]), icloud_root=str(roots[1]),
        dropbox_root=str(roots[2]), verified_at=timezone.now(),
    )
    models.QuerySet.update(Invoice.objects.filter(pk=st_invoice.pk),
        izdajatelj_naziv="Alfa d.o.o.", izdajatelj_davcna_stevilka="15012557",
        status="POSLAN")
    st_invoice.refresh_from_db()
    DeliveryAttempt.objects.create(
        invoice=st_invoice, sequence=1, sender="sender@example.com",
        recipients_to=["buyer@example.com"], result="ACCEPTED",
        pdf_path=st_invoice.rendered_pdf_path, pdf_checksum=st_invoice.checksum,
    )
    for target in (
        "delivery.services.send_pdf_email", "delivery.services.prepare_attempt",
        "delivery.st_delivery.prepare_st_attempt", "rendering.services.render_eslog",
        "companies.services.dodeli_naslednjo_stevilko", "keyring.set_password",
    ):
        monkeypatch.setattr(target, lambda *a, **k: pytest.fail("Unexpected external/issuance effect"))
    return st_invoice


def archive(invoice, **changes):
    return archive_st_invoice(invoice, company_id=invoice.company_id, **changes)


def status(invoice):
    return st_archive_status(invoice, company_id=invoice.company_id)


def protected_data():
    return [list(model.objects.order_by("pk").values()) for model in (
        Invoice, InvoiceLine, DeliveryAttempt, AuditLogEntry, NumberSequence,
        MonthlyCycle, MonthlyBatch, BackupSnapshot,
    )]


def test_exact_snapshot_paths_bytes_checksums_idempotency_and_no_other_effects(sent_st, settings):
    before = protected_data()
    original = settings.RENDERING_ROOT / sent_st.rendered_pdf_path
    pdf, original_mtime = original.read_bytes(), original.stat().st_mtime_ns
    # Current company settings must never replace the issued issuer identity.
    models.QuerySet.update(type(sent_st.company).objects.filter(pk=sent_st.company_id),
        naziv="New company name", davcna_stevilka="99999999")
    assert status(sent_st).state == "READY"
    assert archive(sent_st).verified
    record = ArchivedFile.objects.get()
    paths = [Path(record.local_path), Path(record.icloud_path)]
    mtimes = [path.stat().st_mtime_ns for path in paths]
    assert archive(sent_st).verified
    assert status(sent_st).verified
    assert ArchivedFile.objects.count() == 1
    configuration = ArchiveConfiguration.objects.get()
    for path, root in zip(paths, (configuration.local_root, configuration.icloud_root)):
        assert path.relative_to(root).as_posix() == "Arhiv/Alfa-doo-15012557/2026/09/ST-2026-0001.pdf"
        assert path.read_bytes() == pdf
        assert sha256(path.read_bytes()).hexdigest() == record.checksum == sent_st.checksum
    assert [path.stat().st_mtime_ns for path in paths] == mtimes
    assert original.read_bytes() == pdf and original.stat().st_mtime_ns == original_mtime
    assert protected_data() == before
    assert not settings.FEATURE_ESLOG


@pytest.mark.parametrize("change", [
    {"status": "OSNUTEK"}, {"status": "IZBRAN_ZA_POSILJANJE"},
    {"status": "NAPAKA"}, {"status": "REZULTAT_NI_ZNAN"},
    {"stevilka": ""}, {"stevilka": None}, {"document_type": "NP"},
    {"document_type": "STORNO"}, {"povezan_racun_id": "self"},
])
def test_ineligible_invoices_write_nothing(sent_st, change):
    if change.get("povezan_racun_id") == "self":
        change = {"povezan_racun_id": sent_st.pk}
    models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk), **change)
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()
    config = ArchiveConfiguration.objects.get()
    assert not list(Path(config.local_root).iterdir())
    assert not list(Path(config.icloud_root).iterdir())


@pytest.mark.parametrize("result", ["PREPARED", "CANCELLED", "SUBMISSION_STARTED", "CONFIRMED_FAILURE", "UNKNOWN"])
def test_only_valid_persisted_sent_evidence_allows_archival(sent_st, result):
    DeliveryAttempt.objects.update(result=result)
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


def test_explicit_unknown_resolution_preserves_original_evidence(sent_st):
    DeliveryAttempt.objects.update(result="UNKNOWN")
    models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk), status="REZULTAT_NI_ZNAN")
    resolve_unknown_as_sent(DeliveryAttempt.objects.get(), reason="Preverjeno v mapi Poslano.")
    before = protected_data()
    assert archive(sent_st).verified
    assert protected_data() == before


@pytest.mark.parametrize("field,value", [("pdf_path", "other.pdf"), ("pdf_checksum", "b" * 64)])
def test_mismatched_delivery_fails_before_metadata(sent_st, field, value):
    models.QuerySet.update(DeliveryAttempt.objects.all(), **{field: value})
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


@pytest.mark.parametrize("damage", ["missing", "bytes", "checksum", "path"])
def test_invalid_official_source_is_never_repaired(sent_st, settings, damage):
    original = settings.RENDERING_ROOT / sent_st.rendered_pdf_path
    if damage == "missing":
        original.unlink()
    elif damage == "bytes":
        original.write_bytes(b"corrupt")
    else:
        changes = {"checksum": "0" * 64} if damage == "checksum" else {"rendered_pdf_path": "../secret.pdf"}
        models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk), **changes)
    before = protected_data()
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()
    assert protected_data() == before
    assert original.exists() == (damage != "missing")
    if damage == "bytes":
        assert original.read_bytes() == b"corrupt"


@pytest.mark.parametrize("side", ["local", "icloud"])
def test_partial_failure_and_explicit_retry_reuse_record_and_valid_copy(sent_st, monkeypatch, side):
    from archive import services

    original = services._publish
    configuration = ArchiveConfiguration.objects.get()
    failed_root = Path(getattr(configuration, f"{side}_root"))

    def fail_one(path, *args):
        if path.is_relative_to(failed_root):
            raise OSError("secret filesystem error")
        return original(path, *args)

    with monkeypatch.context() as patch:
        patch.setattr(services, "_publish", fail_one)
        result = archive(sent_st)
    assert not result.verified and result.can_archive and result.retry
    assert "secret" not in result.message
    record = ArchivedFile.objects.get()
    assert record.status == "IN_PROGRESS"
    good = Path(record.icloud_path if side == "local" else record.local_path)
    mtime = good.stat().st_mtime_ns
    before = protected_data()
    assert archive(sent_st).verified
    assert ArchivedFile.objects.get().pk == record.pk
    assert good.stat().st_mtime_ns == mtime
    assert protected_data() == before


@pytest.mark.parametrize("side", ["local", "icloud"])
def test_missing_configured_root_is_not_silently_recreated(sent_st, side):
    config = ArchiveConfiguration.objects.get()
    missing = Path(getattr(config, f"{side}_root"))
    missing.rmdir()
    result = archive(sent_st)
    assert not result.verified
    assert not missing.exists()
    record = ArchivedFile.objects.get()
    good = Path(record.icloud_path if side == "local" else record.local_path)
    assert good.is_file() and record.status == "IN_PROGRESS"
    missing.mkdir()
    assert archive(sent_st).verified


@pytest.mark.parametrize("damage,expected", [
    ("local_missing", "LOCAL_MISSING"), ("icloud_missing", "ICLOUD_MISSING"),
    ("local_corrupt", "LOCAL_CHECKSUM_MISMATCH"),
    ("icloud_corrupt", "ICLOUD_CHECKSUM_MISMATCH"),
])
def test_live_state_never_trusts_historical_verified_flag(sent_st, damage, expected):
    assert archive(sent_st).verified
    record = ArchivedFile.objects.get()
    path = Path(record.local_path if damage.startswith("local") else record.icloud_path)
    if damage.endswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"conflicting copy")
    assert status(sent_st).state == expected
    if damage.endswith("corrupt"):
        assert not archive(sent_st).verified
        assert path.read_bytes() == b"conflicting copy"


def test_metadata_conflict_is_not_repaired_or_read_outside_roots(sent_st, tmp_path, monkeypatch):
    assert archive(sent_st).verified
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"private")
    ArchivedFile.objects.update(local_path=str(outside))
    before = list(ArchivedFile.objects.values())
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != outside, "Conflicting metadata must not be read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert status(sent_st).state == "METADATA_CONFLICT"
    assert not archive(sent_st).verified
    assert list(ArchivedFile.objects.values()) == before


@pytest.mark.parametrize("case", ["absent", "blank", "relative", "overlap", "media", "rendering", "dropbox"])
def test_unsafe_configuration_writes_nothing(sent_st, settings, case):
    config = ArchiveConfiguration.objects.get()
    if case == "absent":
        config.delete()
    else:
        local = {"blank": "", "relative": "relative", "overlap": str(Path(config.icloud_root) / "nested"),
                 "media": str(settings.MEDIA_ROOT), "rendering": str(settings.RENDERING_ROOT),
                 "dropbox": config.dropbox_root}[case]
        ArchiveConfiguration.objects.update(local_root=local)
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


def test_company_scope_and_persisted_state_are_required(sent_st):
    assert not archive_st_invoice(sent_st, company_id=sent_st.company_id + 1).verified
    sent_st.pk = None
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


def test_st_with_a_lease_is_rejected(sent_st):
    models.QuerySet.update(Invoice.objects.filter(pk=sent_st.pk),
        lease_id=sent_st.prejemnik.lease_set.get().pk)
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


def test_latest_attempt_is_required_even_after_earlier_acceptance(sent_st):
    DeliveryAttempt.objects.create(
        invoice=sent_st, sequence=2, sender="sender@example.com",
        recipients_to=["buyer@example.com"], result="PREPARED",
        pdf_path=sent_st.rendered_pdf_path, pdf_checksum=sent_st.checksum,
    )
    assert not archive(sent_st).verified
    assert not ArchivedFile.objects.exists()


def test_interruption_before_metadata_commit_is_completed_only_by_explicit_retry(sent_st):
    assert archive(sent_st).verified
    ArchivedFile.objects.update(status="IN_PROGRESS")
    record = ArchivedFile.objects.get()
    mtimes = [Path(path).stat().st_mtime_ns for path in (record.local_path, record.icloud_path)]
    assert status(sent_st).state == "IN_PROGRESS"
    assert ArchivedFile.objects.get().status == "IN_PROGRESS"
    assert archive(sent_st).verified
    assert [Path(path).stat().st_mtime_ns for path in (record.local_path, record.icloud_path)] == mtimes


def test_unknown_archive_metadata_status_requires_review(sent_st):
    assert archive(sent_st).verified
    ArchivedFile.objects.update(status="INVALID")
    assert status(sent_st).state == "METADATA_CONFLICT"
    assert not archive(sent_st).verified
    assert ArchivedFile.objects.get().status == "INVALID"


@pytest.mark.parametrize("change", [{"checksum": "b" * 64}, {"status": "INVALID"}])
def test_metadata_change_during_publication_is_not_overwritten(sent_st, monkeypatch, change):
    from archive import services

    original = services._publish

    def publish(path, *args):
        original(path, *args)
        ArchivedFile.objects.update(**change)

    monkeypatch.setattr(services, "_publish", publish)
    assert archive(sent_st).state == "METADATA_CONFLICT"
    record = ArchivedFile.objects.get()
    for field, value in change.items():
        assert getattr(record, field) == value
    if "status" not in change:
        assert record.status == "IN_PROGRESS"


def test_symlink_cannot_redirect_archive_outside_configured_root(sent_st, tmp_path):
    config = ArchiveConfiguration.objects.get()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = Path(config.local_root) / "Arhiv"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require OS privileges")
    assert archive(sent_st).state == "METADATA_CONFLICT"
    assert not list(outside.iterdir())
    assert not ArchivedFile.objects.exists()


def test_io_stays_outside_atomic_metadata_sections(sent_st, monkeypatch):
    from archive import services

    for name in ("_publish", "_hash_file"):
        original = getattr(services, name)

        def checked(*args, _original=original, **kwargs):
            assert not connection.in_atomic_block and connection.get_autocommit()
            return _original(*args, **kwargs)

        monkeypatch.setattr(services, name, checked)
    assert archive(sent_st).verified
    with transaction.atomic():
        assert not archive(sent_st).verified


def test_concurrent_attempts_share_metadata_and_exact_copies(sent_st):
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return archive(sent_st)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert all(result.verified for result in results)
    assert ArchivedFile.objects.count() == 1
    assert status(sent_st).verified
