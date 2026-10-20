"""Two-copy archival of corrections, using isolated roots only."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from hashlib import sha256
from pathlib import Path

import pytest
from django.db import models

from archive.configuration import verify_and_save_archive_roots
from archive.models import ArchiveConfiguration, ArchivedFile, BackupSnapshot
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from tests.delivery.test_correction_delivery import (
    correction_factory,
    pogodba,
    prepare,
    send,
    smtp_config,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def archive_roots(tmp_path, settings):
    # The invoice fixture keeps MEDIA_ROOT in its temporary parent directory.
    roots = [tmp_path.parent / (tmp_path.name + "-" + name) for name in ("local", "icloud", "dropbox")]
    for root in roots:
        root.mkdir()
    verify_and_save_archive_roots(*map(str, roots))
    return roots


def archive(invoice):
    from archive.corrections import archive_correction
    return archive_correction(invoice, company_id=invoice.company_id)


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
@pytest.mark.parametrize("source", ["NP", "standalone", "legacy"])
def test_archive_requires_proven_delivery_and_preserves_exact_pdf(correction_factory, archive_roots, smtp_config, settings, kind, source):
    invoice = correction_factory(kind, source)
    assert not archive(invoice).verified
    attempt = prepare(invoice)
    assert not archive(invoice).verified and not ArchivedFile.objects.exists()
    send(attempt, smtp_config)
    assert archive(invoice).verified
    record = ArchivedFile.objects.get()
    pdf = (settings.RENDERING_ROOT / invoice.rendered_pdf_path).read_bytes()
    for value in (record.local_path, record.icloud_path):
        assert Path(value).read_bytes() == pdf
        assert sha256(Path(value).read_bytes()).hexdigest() == record.checksum == invoice.checksum
    assert archive(invoice).verified and ArchivedFile.objects.count() == 1
    assert not BackupSnapshot.objects.exists() and not list(archive_roots[2].iterdir())


def test_partial_archive_continues_explicitly_and_conflicting_bytes_never_replaced(correction_factory, archive_roots, smtp_config, monkeypatch):
    from archive import services
    from archive.corrections import correction_archive_status
    invoice = correction_factory()
    send(prepare(invoice), smtp_config)
    publish = services._publish
    def partial(path, *args):
        if path.is_relative_to(archive_roots[1]):
            raise OSError("secret path")
        return publish(path, *args)
    with monkeypatch.context() as scoped:
        scoped.setattr(services, "_publish", partial)
        state = archive(invoice)
    assert state.state == "ICLOUD_MISSING" and state.can_archive and "secret" not in state.message
    record = ArchivedFile.objects.get()
    local = Path(record.local_path)
    mtime = local.stat().st_mtime_ns
    assert correction_archive_status(invoice, company_id=invoice.company_id).can_archive
    assert not Path(record.icloud_path).exists()
    assert archive(invoice).verified and local.stat().st_mtime_ns == mtime
    local.write_bytes(b"conflicting bytes")
    assert archive(invoice).state == "LOCAL_CHECKSUM_MISMATCH"
    assert local.read_bytes() == b"conflicting bytes" and ArchivedFile.objects.get().checksum == invoice.checksum


@pytest.mark.parametrize("decision", [None, "unknown_sent", "unknown_not_sent"])
def test_unknown_archive_requires_verified_sent_resolution(correction_factory, archive_roots, smtp_config, decision):
    from delivery.corrections import correction_delivery_action
    from tests.delivery.test_correction_delivery import FakeSmtp
    invoice = correction_factory()
    attempt = send(prepare(invoice), smtp_config, FakeSmtp({"data": TimeoutError()}))
    if decision:
        correction_delivery_action(attempt, action=decision, reason="Ponudnik je preveril rezultat oddaje.")
    result = archive(invoice)
    assert result.verified == (decision == "unknown_sent")
    assert DeliveryAttempt.objects.get(pk=attempt.pk).result == "UNKNOWN"


@pytest.mark.parametrize("fault", ["source", "latest", "metadata", "overlap", "rendering", "escape", "root_missing"])
def test_archive_blocks_invalid_evidence_paths_or_metadata(correction_factory, archive_roots, smtp_config, settings, fault):
    invoice = correction_factory()
    attempt = prepare(invoice)
    send(attempt, smtp_config)
    if fault == "source":
        (settings.RENDERING_ROOT / invoice.rendered_pdf_path).write_bytes(b"corrupt")
    elif fault == "latest":
        DeliveryAttempt.objects.create(invoice=invoice, sequence=2, sender=attempt.sender,
            recipients_to=attempt.recipients_to, pdf_path=attempt.pdf_path, pdf_checksum=attempt.pdf_checksum)
    elif fault == "metadata":
        assert archive(invoice).verified
        ArchivedFile.objects.update(checksum="b" * 64)
    elif fault in {"overlap", "rendering"}:
        ArchiveConfiguration.objects.update(local_root=str(archive_roots[1] if fault == "overlap" else settings.RENDERING_ROOT))
    elif fault == "escape":
        models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), rendered_pdf_path="../private.pdf")
    else:
        archive_roots[1].rmdir()
    assert not archive(invoice).verified
    if fault not in {"metadata", "root_missing"}:
        assert not ArchivedFile.objects.exists()
    if fault == "metadata":
        assert ArchivedFile.objects.get().checksum == "b" * 64
    if fault == "root_missing":
        assert not archive_roots[1].exists()
        archive_roots[1].mkdir()
        assert archive(invoice).verified


def test_generic_archive_rejects_correction_without_review_evidence(correction_factory, archive_roots):
    from archive.services import ArchiveRoots, archive_pdf
    invoice = correction_factory()
    models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), status="POSLAN")
    DeliveryAttempt.objects.create(invoice=invoice, sequence=1, sender="racuni@example.com",
        recipients_to=["buyer@example.com"], result="ACCEPTED", pdf_path=invoice.rendered_pdf_path,
        pdf_checksum=invoice.checksum)
    assert not archive_pdf(invoice, ArchiveRoots(*archive_roots[:2])).success
    assert not ArchivedFile.objects.exists()
