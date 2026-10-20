"""Explicit two-copy archival of the registered correction PDF."""

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import DatabaseError, connection

from delivery.corrections import validate_correction_attempt
from delivery.services import has_sent_delivery_evidence
from invoicing.models import Invoice

from . import services
from .configuration import ArchiveConfigurationError
from .models import ArchivedFile
from .st_invoices import STArchiveStatus, _configured_roots, _status


def _ineligible():
    return STArchiveStatus("NOT_ELIGIBLE", "Arhiviranje zahteva izdano korekcijo in veljavno dokazilo uspešne dostave.")


def _inspect(invoice, company_id):
    if connection.in_atomic_block or not connection.get_autocommit():
        return None, None, _status("FAILED")
    current = Invoice.objects.filter(pk=invoice.pk, company_id=company_id).first()
    if current is None or not has_sent_delivery_evidence(current):
        return None, None, _ineligible()
    attempt = current.delivery_attempts.order_by("-sequence").first()
    validate_correction_attempt(attempt)
    roots = _configured_roots()
    paths = services.archive_paths(current, roots)
    archived = ArchivedFile.objects.filter(invoice=current, artifact_type=ArchivedFile.ArtifactType.PDF).first()
    if archived is None:
        return current, roots, _status("READY", can_archive=True)
    if archived.status not in ArchivedFile.Status.values or (
        archived.local_path, archived.icloud_path, archived.checksum
    ) != (str(paths[0]), str(paths[1]), current.checksum):
        return None, None, _status("METADATA_CONFLICT")
    verification = services.verify_archived_file(archived, roots=roots)
    if verification.success and archived.status != ArchivedFile.Status.VERIFIED:
        return current, roots, _status("IN_PROGRESS", can_archive=True, retry=True)
    state = verification.state
    if state == "BOTH_MISSING" and archived.status == ArchivedFile.Status.IN_PROGRESS:
        state = "IN_PROGRESS"
    can_archive = state in {"IN_PROGRESS", "BOTH_MISSING", "LOCAL_MISSING", "ICLOUD_MISSING", "FAILED"}
    return current, roots, _status(state, can_archive=can_archive, retry=can_archive)


def _operate(invoice, company_id, *, write):
    try:
        current, roots, state = _inspect(invoice, company_id)
        if not write or not state.can_archive:
            return state
        result = services.archive_pdf(current, roots)
        return _status(result.state, can_archive=not result.success and result.state in {
            "IN_PROGRESS", "BOTH_MISSING", "LOCAL_MISSING", "ICLOUD_MISSING", "FAILED",
        }, retry=not result.success)
    except ValidationError:
        return _status("SOURCE_INVALID")
    except (ArchiveConfigurationError, services.ArchiveConflict, ValueError, RuntimeError):
        return _status("METADATA_CONFLICT")
    except (OSError, DatabaseError, ObjectDoesNotExist):
        return _status("FAILED")


def correction_archive_status(invoice, *, company_id):
    return _operate(invoice, company_id, write=False)


def archive_correction(invoice, *, company_id):
    return _operate(invoice, company_id, write=True)
