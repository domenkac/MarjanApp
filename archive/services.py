import hashlib
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import connection, transaction

from core.pravila import slugify_naziv
from delivery.models import DeliveryAttempt
from delivery.services import has_sent_delivery_evidence
from invoicing.models import Invoice
from rendering.services import stanje_koncnega_pdf

from .models import ArchivedFile

_SHA256 = re.compile(r"[0-9a-f]{64}")


class ArchiveState(StrEnum):
    VERIFIED = "VERIFIED"
    LOCAL_MISSING = "LOCAL_MISSING"
    ICLOUD_MISSING = "ICLOUD_MISSING"
    LOCAL_CHECKSUM_MISMATCH = "LOCAL_CHECKSUM_MISMATCH"
    ICLOUD_CHECKSUM_MISMATCH = "ICLOUD_CHECKSUM_MISMATCH"
    BOTH_MISSING = "BOTH_MISSING"
    METADATA_CONFLICT = "METADATA_CONFLICT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ArchiveRoots:
    local_root: Path
    icloud_root: Path

    def __post_init__(self):
        local = Path(self.local_root)
        icloud = Path(self.icloud_root)
        if not local.is_absolute() or not icloud.is_absolute():
            raise ValueError("Archive roots must be absolute paths.")
        if local.resolve(strict=False) == icloud.resolve(strict=False):
            raise ValueError("Archive roots must be distinct.")
        object.__setattr__(self, "local_root", local.resolve(strict=False))
        object.__setattr__(self, "icloud_root", icloud.resolve(strict=False))


@dataclass(frozen=True, slots=True)
class ArchiveVerification:
    success: bool
    state: ArchiveState
    message: str
    archived_file_id: int | None
    local_path: str
    icloud_path: str
    checksum: str


ArchiveResult = ArchiveVerification


class ArchiveConflict(ValueError):
    pass


def _safe_component(value: str, label: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value or ""):
        raise ArchiveConflict(f"Unsafe {label} archive path component.")
    value = (value or "").strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise ArchiveConflict(f"Unsafe {label} archive path component.")
    return value


def pdf_relative_path(invoice: Invoice) -> Path:
    issuer_name = _safe_component(invoice.izdajatelj_naziv or "", "company")
    issuer_tax_number = _safe_component(
        invoice.izdajatelj_davcna_stevilka or "", "issuer identity"
    )
    company = _safe_component(
        f"{slugify_naziv(issuer_name)}-{issuer_tax_number}", "company"
    )
    number = _safe_component(invoice.stevilka or "", "invoice number")
    return Path(
        "Arhiv",
        company,
        str(invoice.datum_izdaje.year),
        f"{invoice.datum_izdaje.month:02d}",
        f"{number}.pdf",
    )


def _target(root: Path, relative: Path) -> Path:
    target = (root / relative).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ArchiveConflict("Archive target escapes its configured root.")
    return target


def archive_paths(invoice: Invoice, roots: ArchiveRoots) -> tuple[Path, Path]:
    relative = pdf_relative_path(invoice)
    return _target(roots.local_root, relative), _target(roots.icloud_root, relative)


def _result(state, message, archived=None, *, paths=("", ""), checksum=""):
    return ArchiveResult(
        state == ArchiveState.VERIFIED,
        state,
        message,
        archived.pk if archived else None,
        str(paths[0]),
        str(paths[1]),
        checksum,
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish(path: Path, data: bytes, checksum: str) -> None:
    if path.exists():
        if _hash_file(path) != checksum:
            raise ArchiveConflict(f"Existing archive file has wrong checksum: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _hash_file(path) != checksum:
                raise ArchiveConflict(
                    f"Existing archive file has wrong checksum: {path}"
                )
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    if _hash_file(path) != checksum:
        raise ArchiveConflict(f"Published archive file has wrong checksum: {path}")


def _delivery_evidence(invoice: Invoice) -> DeliveryAttempt:
    if invoice.status != Invoice.Status.POSLAN:
        raise ArchiveConflict("Invoice is not sent.")
    attempt = invoice.delivery_attempts.order_by("-sequence").first()
    if attempt is None:
        raise ArchiveConflict("Invoice has no delivery attempt.")
    if not has_sent_delivery_evidence(invoice, attempt):
        raise ArchiveConflict("Latest delivery attempt does not prove delivery.")
    return attempt


def _verified_source(invoice: Invoice) -> tuple[Invoice, bytes, DeliveryAttempt]:
    if invoice.pk is None:
        raise ArchiveConflict("Invoice must be persisted.")
    invoice = Invoice.objects.get(pk=invoice.pk)
    if not invoice.stevilka:
        raise ArchiveConflict("Invoice has no issued number.")
    if not _SHA256.fullmatch(invoice.checksum or ""):
        raise ArchiveConflict("Invoice checksum is invalid.")
    attempt = _delivery_evidence(invoice)
    if invoice.document_type in (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS):
        from delivery.corrections import validate_correction_attempt

        try:
            validate_correction_attempt(attempt)
        except (ValidationError, ValueError, ObjectDoesNotExist) as error:
            raise ArchiveConflict("Correction delivery evidence is invalid.") from error
    state = stanje_koncnega_pdf(invoice)
    if state["state"] != "READY":
        raise ArchiveConflict(
            "Final PDF is not ready or failed integrity verification."
        )
    result = state["result"]
    if result.pot != invoice.rendered_pdf_path or result.checksum != invoice.checksum:
        raise ArchiveConflict("Final PDF metadata differs from invoice metadata.")
    if hashlib.sha256(result.pdf_bytes).hexdigest() != invoice.checksum:
        raise ArchiveConflict("Final PDF bytes differ from invoice checksum.")
    return invoice, result.pdf_bytes, attempt


def archive_pdf(
    invoice: Invoice, roots: ArchiveRoots, *, require_existing_roots: bool = False
) -> ArchiveResult:
    archived = None
    paths = ("", "")
    checksum = ""
    try:
        if connection.in_atomic_block:
            raise ArchiveConflict(
                "Archive operation cannot run inside a DB transaction."
            )
        invoice, source, attempt = _verified_source(invoice)
        if invoice.document_type in (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS):
            from .st_invoices import _configured_roots

            if roots != _configured_roots():
                raise ArchiveConflict("Correction archive roots differ from configured roots.")
        checksum = invoice.checksum
        paths = archive_paths(invoice, roots)
        identity = {
            "local_path": str(paths[0]),
            "icloud_path": str(paths[1]),
            "checksum": checksum,
        }
        with transaction.atomic():
            archived, created = ArchivedFile.objects.get_or_create(
                invoice=invoice,
                artifact_type=ArchivedFile.ArtifactType.PDF,
                defaults={**identity, "status": ArchivedFile.Status.IN_PROGRESS},
            )
            conflicts = [
                key
                for key, value in identity.items()
                if getattr(archived, key) != value
            ]
            if not created and conflicts:
                raise ArchiveConflict(
                    "Existing archive metadata conflicts with this archive identity: "
                    + ", ".join(conflicts)
                )

        errors = []
        for label, path, root in (
            ("local", paths[0], roots.local_root),
            ("iCloud", paths[1], roots.icloud_root),
        ):
            try:
                # ST must not recreate a missing mount or configured root.
                if require_existing_roots and not root.is_dir():
                    raise OSError("Configured archive root is unavailable.")
                _publish(path, source, checksum)
            except (OSError, ArchiveConflict) as error:
                errors.append(f"{label}: {error}")
        verification = verify_archived_file(archived)
        if not verification.success:
            return _result(
                verification.state,
                "; ".join(errors) or verification.message,
                archived,
                paths=paths,
                checksum=checksum,
            )
        if attempt.pdf_checksum != checksum:
            raise ArchiveConflict("Delivery checksum changed during archival.")
        with transaction.atomic():
            updated = ArchivedFile.objects.filter(
                pk=archived.pk, status__in=ArchivedFile.Status.values, **identity
            ).update(
                status=ArchivedFile.Status.VERIFIED
            )
            if updated != 1:
                raise ArchiveConflict("Archive metadata changed during archival.")
        archived.status = ArchivedFile.Status.VERIFIED
        return _result(
            ArchiveState.VERIFIED,
            "Both archive copies are verified.",
            archived,
            paths=paths,
            checksum=checksum,
        )
    except (ArchiveConflict, OSError) as error:
        return _result(
            ArchiveState.METADATA_CONFLICT
            if isinstance(error, ArchiveConflict)
            else ArchiveState.FAILED,
            str(error),
            archived,
            paths=paths,
            checksum=checksum,
        )


def verify_archived_file(
    archived_file: ArchivedFile, *, roots: ArchiveRoots | None = None
) -> ArchiveVerification:
    paths = (Path(archived_file.local_path), Path(archived_file.icloud_path))
    checksum = archived_file.checksum
    if (
        connection.in_atomic_block
        or archived_file.artifact_type != ArchivedFile.ArtifactType.PDF
        or not _SHA256.fullmatch(checksum or "")
        or checksum != archived_file.invoice.checksum
        or not all(path.is_absolute() for path in paths)
        or paths[0] == paths[1]
    ):
        return _result(
            ArchiveState.METADATA_CONFLICT,
            "Archive metadata is invalid.",
            archived_file,
            paths=paths,
            checksum=checksum,
        )
    try:
        if roots is not None and paths != archive_paths(archived_file.invoice, roots):
            raise ArchiveConflict("Archive paths differ from the configured identity.")
        exists = tuple(path.exists() for path in paths)
        hashes = tuple(
            _hash_file(path) if present else None
            for path, present in zip(paths, exists)
        )
        if exists == (False, False):
            return _result(
                ArchiveState.BOTH_MISSING,
                "Both archive copies are missing.",
                archived_file,
                paths=paths,
                checksum=checksum,
            )
        if not exists[0]:
            return _result(
                ArchiveState.LOCAL_MISSING,
                "Local archive copy is missing.",
                archived_file,
                paths=paths,
                checksum=checksum,
            )
        if not exists[1]:
            return _result(
                ArchiveState.ICLOUD_MISSING,
                "iCloud archive copy is missing.",
                archived_file,
                paths=paths,
                checksum=checksum,
            )
        if hashes[0] != checksum:
            return _result(
                ArchiveState.LOCAL_CHECKSUM_MISMATCH,
                "Local archive checksum differs.",
                archived_file,
                paths=paths,
                checksum=checksum,
            )
        if hashes[1] != checksum:
            return _result(
                ArchiveState.ICLOUD_CHECKSUM_MISMATCH,
                "iCloud archive checksum differs.",
                archived_file,
                paths=paths,
                checksum=checksum,
            )
    except ArchiveConflict:
        return _result(
            ArchiveState.METADATA_CONFLICT,
            "Archive paths differ from the configured identity.",
            archived_file,
            paths=paths,
            checksum=checksum,
        )
    except OSError:
        return _result(
            ArchiveState.FAILED,
            "Archive verification failed because a copy is inaccessible.",
            archived_file,
            paths=paths,
            checksum=checksum,
        )
    return _result(
        ArchiveState.VERIFIED,
        "Both archive copies are verified.",
        archived_file,
        paths=paths,
        checksum=checksum,
    )
