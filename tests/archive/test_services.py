# ruff: noqa: F401, F811
from copy import copy
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection

from archive.configuration import resolve_archive_roots
from archive.models import ArchivedFile
from archive.services import (
    ArchiveRoots,
    ArchiveState,
    archive_pdf,
    pdf_relative_path,
    verify_archived_file,
)
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from delivery.services import UNKNOWN_RESOLUTION_FIELD, UNKNOWN_RESOLUTION_SENT_VERIFIED
from invoicing.models import Invoice
from rendering.services import render_pdf, stanje_koncnega_pdf
from tests.rendering.test_contexts import izdani_racun

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sent_invoice(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    pdf = render_pdf(izdani_racun)
    attempt = DeliveryAttempt.objects.create(
        invoice=izdani_racun,
        sequence=1,
        sender="sender@example.com",
        recipients_to=["recipient@example.com"],
        pdf_path=pdf.pot,
        pdf_checksum=pdf.checksum,
        result=DeliveryAttempt.Result.ACCEPTED,
    )
    Invoice.objects.filter(pk=izdani_racun.pk).update(status=Invoice.Status.POSLAN)
    izdani_racun.refresh_from_db()
    return izdani_racun, attempt, pdf.pdf_bytes


@pytest.fixture
def roots(sent_invoice):
    # Issuance setup already probes and saves these isolated synthetic roots.
    return resolve_archive_roots()


def test_archive_happy_path_is_idempotent_and_immutable(sent_invoice, roots):
    invoice, attempt, pdf_bytes = sent_invoice
    before_invoice = Invoice.objects.values().get(pk=invoice.pk)
    before_attempt = DeliveryAttempt.objects.values().get(pk=attempt.pk)

    first = archive_pdf(invoice, roots)
    archived = ArchivedFile.objects.get()
    mtimes = (
        Path(first.local_path).stat().st_mtime_ns,
        Path(first.icloud_path).stat().st_mtime_ns,
    )
    second = archive_pdf(invoice, roots)

    assert first.success and second.success, second.message
    assert archived.status == ArchivedFile.Status.VERIFIED
    assert (
        Path(first.local_path).read_bytes()
        == Path(first.icloud_path).read_bytes()
        == pdf_bytes
    )
    assert Path(first.local_path).relative_to(roots.local_root) == Path(
        first.icloud_path
    ).relative_to(roots.icloud_root)
    assert (
        "Arhiv/Alfa-doo-15012557/2026/09/NP-00001.pdf"
        == Path(first.local_path).relative_to(roots.local_root).as_posix()
    )
    assert mtimes == (
        Path(first.local_path).stat().st_mtime_ns,
        Path(first.icloud_path).stat().st_mtime_ns,
    )
    assert before_invoice == Invoice.objects.values().get(pk=invoice.pk)
    assert before_attempt == DeliveryAttempt.objects.values().get(pk=attempt.pk)


def test_configuration_edits_preserve_issued_invoice_assets_and_archive_identity(sent_invoice, roots, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from archive.configuration import verify_and_save_archive_roots
    from companies.services import save_company_edit, veljavna_potrditev_vzorca
    from core.services import record_edit_fingerprint
    from delivery.configuration import save_monthly_summary_configuration
    from delivery.models import MonthlySummaryConfiguration
    from invoicing.models import InvoiceLine
    from tests.image_data import slika_bytes

    invoice, _, pdf_bytes = sent_invoice
    assert archive_pdf(invoice, roots).success
    archived = ArchivedFile.objects.get(invoice=invoice)
    historical = ArchivedFile.objects.filter(pk=archived.pk).values().get()
    frozen = Invoice.objects.filter(pk=invoice.pk).values().get()
    lines = list(InvoiceLine.objects.filter(invoice=invoice).values())
    company = invoice.company
    company.refresh_from_db()
    token = record_edit_fingerprint(company)
    old_logo = company.logotip.path
    old_logo_bytes = Path(old_logo).read_bytes()
    company.email_racunovodja = "new-accountant@example.com"
    company.email_posiljatelj = "new-sender@example.com"
    company.keychain_kljuc_posiljatelja = "private-new-reference"
    company.logotip = SimpleUploadedFile("new.png", slika_bytes(), content_type="image/png")
    save_company_edit(company, source_fingerprint=token, razlog="Menjava nastavitev")
    assert veljavna_potrditev_vzorca(company).stanje == "stale"
    summary = MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="key")
    token = record_edit_fingerprint(summary)
    summary.email = "new-owner@example.com"
    save_monthly_summary_configuration(summary, source_fingerprint=token, razlog="Menjava predala")
    replacements = [tmp_path.parent / f"{tmp_path.name}-{name}" for name in (
        "new-local", "new-icloud", "new-dropbox",
    )]
    for root in replacements:
        root.mkdir()
    verify_and_save_archive_roots(*replacements, razlog="Menjava lokacij")
    result = archive_pdf(invoice, ArchiveRoots(*replacements[:2]))
    assert result.state == ArchiveState.METADATA_CONFLICT
    assert ArchivedFile.objects.filter(pk=archived.pk).values().get() == historical
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == frozen
    assert list(InvoiceLine.objects.filter(invoice=invoice).values()) == lines
    assert Path(archived.local_path).read_bytes() == Path(archived.icloud_path).read_bytes() == pdf_bytes
    pdf_state = stanje_koncnega_pdf(invoice)
    assert pdf_state["state"] == "READY"
    assert pdf_state["result"].pdf_bytes == pdf_bytes
    assert Path(old_logo).read_bytes() == old_logo_bytes
    assert all(not list(root.iterdir()) for root in replacements)


def test_resolved_unknown_is_accepted(sent_invoice, roots):
    invoice, attempt, _ = sent_invoice
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(
        result=DeliveryAttempt.Result.UNKNOWN
    )
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(DeliveryAttempt),
        object_id=attempt.pk,
        polje=UNKNOWN_RESOLUTION_FIELD,
        stara_vrednost="UNRESOLVED",
        nova_vrednost=UNKNOWN_RESOLUTION_SENT_VERIFIED,
        razlog="Checked",
    )
    assert archive_pdf(invoice, roots).success
    assert (
        DeliveryAttempt.objects.get(pk=attempt.pk).result
        == DeliveryAttempt.Result.UNKNOWN
    )


@pytest.mark.parametrize(
    "case",
    [
        "not_sent",
        "no_attempt",
        "failed",
        "unknown",
        "forged_unknown",
        "forged_unknown_action",
        "snapshot",
    ],
)
def test_invalid_delivery_evidence_writes_nothing(sent_invoice, roots, case):
    invoice, attempt, _ = sent_invoice
    if case == "not_sent":
        Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.NAPAKA)
    elif case == "no_attempt":
        DeliveryAttempt.objects.filter(pk=attempt.pk).delete()
    elif case == "failed":
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(
            result=DeliveryAttempt.Result.CONFIRMED_FAILURE
        )
    elif case in {"unknown", "forged_unknown", "forged_unknown_action"}:
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(
            result=DeliveryAttempt.Result.UNKNOWN
        )
        if case in {"forged_unknown", "forged_unknown_action"}:
            audit = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(DeliveryAttempt),
                object_id=attempt.pk,
                dejanje=(
                    AuditLogEntry.Dejanje.FIZICNI_IZBRIS
                    if case == "forged_unknown_action"
                    else AuditLogEntry.Dejanje.SPREMEMBA
                ),
                polje=UNKNOWN_RESOLUTION_FIELD,
                stara_vrednost=(
                    "UNRESOLVED" if case == "forged_unknown_action" else "OTHER"
                ),
                nova_vrednost=UNKNOWN_RESOLUTION_SENT_VERIFIED,
                razlog="Invalid transition",
            )
            if case == "forged_unknown_action":
                with pytest.raises(ValidationError):
                    audit.save()
                # Legacy corrupt evidence must remain unusable by the archive.
                audit.save_base(force_insert=True)
            else:
                audit.save()
    else:
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(pdf_checksum="b" * 64)
    result = archive_pdf(invoice, roots)
    assert not result.success
    assert not ArchivedFile.objects.exists()
    assert not list(roots.local_root.iterdir()) and not list(roots.icloud_root.iterdir())


@pytest.mark.parametrize("case", ["missing", "corrupt", "checksum", "number"])
def test_invalid_source_is_rejected(sent_invoice, roots, settings, case):
    invoice, _, _ = sent_invoice
    source = settings.RENDERING_ROOT / invoice.rendered_pdf_path
    if case == "missing":
        source.unlink()
    elif case == "corrupt":
        source.write_bytes(b"bad")
    elif case == "checksum":
        Invoice.objects.filter(pk=invoice.pk).update(checksum="bad")
    else:
        Invoice.objects.filter(pk=invoice.pk).update(stevilka="")
    assert not archive_pdf(invoice, roots).success
    assert not ArchivedFile.objects.exists()


@pytest.mark.parametrize("failed_side", ["local", "icloud"])
def test_one_failed_destination_is_retained_and_retry_resumes(
    sent_invoice, roots, monkeypatch, failed_side
):
    invoice, _, source = sent_invoice
    from archive import services

    original = services._publish
    failed_root = getattr(roots, f"{failed_side}_root")
    good_root = roots.icloud_root if failed_side == "local" else roots.local_root

    def publish(path, *args, **kwargs):
        if path.is_relative_to(failed_root):
            raise OSError(f"{failed_side} unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(services, "_publish", publish)
        failed = archive_pdf(invoice, roots)
    good = good_root / Path(failed.local_path).relative_to(roots.local_root)
    mtime = good.stat().st_mtime_ns
    assert not failed.success and good.read_bytes() == source
    assert ArchivedFile.objects.get().status == ArchivedFile.Status.IN_PROGRESS
    assert archive_pdf(invoice, roots).success
    assert good.stat().st_mtime_ns == mtime


def test_wrong_existing_copy_is_not_overwritten(sent_invoice, roots):
    invoice, _, _ = sent_invoice
    target = (
        roots.local_root
        / "Arhiv"
        / "Alfa-doo-15012557"
        / "2026"
        / "09"
        / f"{invoice.stevilka}.pdf"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"suspicious")
    result = archive_pdf(invoice, roots)
    assert not result.success
    assert target.read_bytes() == b"suspicious"
    assert ArchivedFile.objects.get().status == ArchivedFile.Status.IN_PROGRESS


def test_live_verification_detects_stale_verified_state(sent_invoice, roots):
    invoice, _, _ = sent_invoice
    result = archive_pdf(invoice, roots)
    archived = ArchivedFile.objects.get()
    Path(result.local_path).unlink()
    assert verify_archived_file(archived).state == ArchiveState.LOCAL_MISSING
    Path(result.local_path).write_bytes(Path(result.icloud_path).read_bytes())
    Path(result.icloud_path).write_bytes(b"changed")
    assert verify_archived_file(archived).state == ArchiveState.ICLOUD_CHECKSUM_MISMATCH
    assert ArchivedFile.objects.get().status == ArchivedFile.Status.VERIFIED


def test_filesystem_operations_are_outside_transactions(
    sent_invoice, roots, monkeypatch
):
    invoice, _, _ = sent_invoice
    from archive import services

    real_publish, real_hash = services._publish, services._hash_file
    monkeypatch.setattr(
        services,
        "_publish",
        lambda *a, **k: (assert_not_atomic(), real_publish(*a, **k))[1],
    )
    monkeypatch.setattr(
        services,
        "_hash_file",
        lambda *a, **k: (assert_not_atomic(), real_hash(*a, **k))[1],
    )
    assert archive_pdf(invoice, roots).success


def test_publish_error_after_correct_write_still_finishes_verified(
    sent_invoice, roots, monkeypatch
):
    invoice, _, _ = sent_invoice
    from archive import services

    real_publish = services._publish

    def publish_then_error(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise OSError("transient publication error")

    monkeypatch.setattr(services, "_publish", publish_then_error)
    result = archive_pdf(invoice, roots)

    assert result.success
    assert result.state == ArchiveState.VERIFIED
    archived = ArchivedFile.objects.get()
    assert archived.status == ArchivedFile.Status.VERIFIED
    assert verify_archived_file(archived).success


def assert_not_atomic():
    assert connection.in_atomic_block is False


@pytest.mark.parametrize(
    "company,number",
    [
        ("..", "NP-1"),
        ("bad/name", "NP-1"),
        ("Company", "../NP-1"),
        ("", "NP-1"),
        ("Com\x00pany", "NP-1"),
        ("Com\x1fpany", "NP-1"),
        ("Company", "NP\x00-1"),
        ("Company", "NP\x7f-1"),
    ],
)
def test_unsafe_components_cannot_escape(sent_invoice, roots, company, number):
    invoice, _, _ = sent_invoice
    Invoice.objects.filter(pk=invoice.pk).update(
        izdajatelj_naziv=company, stevilka=number
    )
    assert not archive_pdf(invoice, roots).success
    assert not ArchivedFile.objects.exists()
    assert not list(roots.local_root.iterdir()) and not list(roots.icloud_root.iterdir())


def test_company_archive_component_uses_immutable_issuer_identity(sent_invoice):
    first, _, _ = sent_invoice
    second = copy(first)
    second.izdajatelj_naziv = "Alfa doo"
    second.izdajatelj_davcna_stevilka = "99999999"

    first_path = pdf_relative_path(first)
    second_path = pdf_relative_path(second)

    assert first_path != second_path
    assert first_path.parts[:1] == second_path.parts[:1] == ("Arhiv",)
    assert first_path.parts[2:] == second_path.parts[2:] == (
        "2026",
        "09",
        "NP-00001.pdf",
    )


def test_roots_must_be_absolute_and_distinct(tmp_path):
    with pytest.raises(ValueError):
        ArchiveRoots(Path("relative"), tmp_path.resolve())
    with pytest.raises(ValueError):
        ArchiveRoots(tmp_path.resolve(), tmp_path.resolve())
