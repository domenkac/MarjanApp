"""Local, immutable accountant PDF artifacts; independent of invoice archives."""

import hashlib
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from core.pravila import slugify_naziv

from .models import AccountantReport, AccountantReportPdfArtifact
from .pdf_renderer import (
    prepare_accountant_report_context,
    render_accountant_report_pdf,
)


class PdfArchiveState(StrEnum):
    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    METADATA_CONFLICT = "METADATA_CONFLICT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PdfArchiveResult:
    state: PdfArchiveState
    message: str
    artifact_id: int | None = None
    local_path: str = ""
    checksum: str = ""

    @property
    def success(self) -> bool:
        return self.state == PdfArchiveState.VERIFIED


class PdfArchiveConflict(ValueError):
    pass


def _root(local_root: Path) -> Path:
    root = Path(local_root)
    if not root.is_absolute():
        raise PdfArchiveConflict("Local archive root must be absolute.")
    return root.resolve(strict=False)


def _safe_component(value: str) -> str:
    value = value.strip()
    if (
        not value or value in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"/\\|?*' for char in value)
        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0], re.IGNORECASE)
    ):
        raise PdfArchiveConflict("Unsafe historical archive path component.")
    return value


def accountant_report_pdf_relative_path(report: AccountantReport) -> Path:
    """Technical identity includes report ID to separate snapshots of the same month."""
    if type(report.pk) is not int or report.pk <= 0:
        raise PdfArchiveConflict("Accountant report must be persisted.")
    context = prepare_accountant_report_context(report)
    name = _safe_component(slugify_naziv(_safe_component(context["company_name"])))
    tax = _safe_component(context["company_tax_number"])
    registration = _safe_component(context["company_registration_number"])
    period = context["period"]
    year, month = period.split("-")
    return Path(
        "Kontrolni-dokumenti", f"{name}-{tax}-{registration}", year, month,
        f"report-{report.pk}", f"{period}-{name}.pdf",
    )


def _target(root: Path, report: AccountantReport) -> Path:
    target = root / accountant_report_pdf_relative_path(report)
    if not target.resolve(strict=False).is_relative_to(root):
        raise PdfArchiveConflict("PDF archive path escapes local root.")
    return target


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish(path: Path, data: bytes, checksum: str) -> None:
    """Publish a complete file atomically, without replacing an existing target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if _hash_file(path) != checksum:
        raise PdfArchiveConflict("Existing or published PDF has a conflicting checksum.")


def _result(state, message, artifact=None, *, path="", checksum=""):
    return PdfArchiveResult(
        state, message, artifact.pk if artifact else None,
        artifact.local_path if artifact else str(path),
        artifact.checksum if artifact else checksum,
    )


def verify_accountant_report_pdf_artifact(
    artifact: AccountantReportPdfArtifact, local_root: Path,
) -> PdfArchiveResult:
    """Check persisted metadata, snapshot-derived location and final on-disk SHA-256."""
    try:
        if connection.in_atomic_block:
            raise PdfArchiveConflict("PDF verification cannot run inside a DB transaction.")
        root = _root(local_root)
        stored = AccountantReportPdfArtifact.objects.filter(pk=artifact.pk).first()
        if stored is None or any(
            getattr(stored, field) != getattr(artifact, field)
            for field in ("report_id", "local_path", "checksum", "verified_at", "created_at")
        ):
            raise PdfArchiveConflict("Artifact metadata differs from persisted metadata.")
        report = AccountantReport.objects.only("snapshot").get(pk=artifact.report_id)
        path = _target(root, report)
        if (
            artifact.local_path != str(path)
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.checksum or "")
            or artifact.verified_at is None or timezone.is_naive(artifact.verified_at)
        ):
            raise PdfArchiveConflict("Invalid PDF artifact metadata.")
        try:
            actual = _hash_file(path)
        except FileNotFoundError:
            return _result(PdfArchiveState.MISSING, "Archived PDF is missing.", artifact)
        if actual != artifact.checksum:
            return _result(PdfArchiveState.CHECKSUM_MISMATCH, "PDF checksum differs.", artifact)
        return _result(PdfArchiveState.VERIFIED, "Local PDF is verified.", artifact)
    except (PdfArchiveConflict, ValidationError, AccountantReport.DoesNotExist) as exc:
        return _result(PdfArchiveState.METADATA_CONFLICT, str(exc), artifact)
    except OSError as exc:
        return _result(PdfArchiveState.FAILED, str(exc), artifact)


def archive_accountant_report_pdf(
    report: AccountantReport, local_root: Path,
) -> PdfArchiveResult:
    """Verify existing artifacts or publish first, then persist immutable metadata."""
    artifact = None
    path = ""
    checksum = ""
    try:
        if connection.in_atomic_block:
            raise PdfArchiveConflict("PDF archive cannot run inside a DB transaction.")
        if type(report.pk) is not int or report.pk <= 0 or report._state.adding:
            raise PdfArchiveConflict("Accountant report must be persisted.")
        root = _root(local_root)
        # Caller objects may be stale or edited in memory. Read persisted snapshot only.
        report = AccountantReport.objects.only("snapshot").get(pk=report.pk)
        path = _target(root, report)
        artifact = AccountantReportPdfArtifact.objects.filter(report_id=report.pk).first()
        if artifact is not None:
            return verify_accountant_report_pdf_artifact(artifact, root)
        pdf = render_accountant_report_pdf(report)
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
            raise PdfArchiveConflict("Renderer did not return PDF bytes.")
        checksum = hashlib.sha256(pdf).hexdigest()
        # Recheck containment immediately before filesystem publication.
        path = _target(root, report)
        _publish(path, pdf, checksum)
        if _hash_file(path) != checksum:
            raise PdfArchiveConflict("Final PDF checksum differs from rendered bytes.")
        verified_at = timezone.now()
        # No rendering, filesystem operations or hashing within this transaction.
        with transaction.atomic():
            artifact, _ = AccountantReportPdfArtifact.objects.get_or_create(
                report_id=report.pk,
                defaults={"local_path": str(path), "checksum": checksum, "verified_at": verified_at},
            )
            if artifact.local_path != str(path) or artifact.checksum != checksum:
                raise PdfArchiveConflict("Existing immutable PDF metadata conflicts.")
        return verify_accountant_report_pdf_artifact(artifact, root)
    except (PdfArchiveConflict, ValidationError, AccountantReport.DoesNotExist) as exc:
        return _result(
            PdfArchiveState.METADATA_CONFLICT, str(exc), artifact, path=path, checksum=checksum,
        )
    except OSError as exc:
        return _result(PdfArchiveState.FAILED, str(exc), artifact, path=path, checksum=checksum)
