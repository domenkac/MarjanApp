"""Operational standalone ST archival through the existing two-copy archive."""

from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, connection

from delivery.services import has_sent_delivery_evidence
from invoicing.models import Invoice
from rendering.services import stanje_koncnega_pdf

from . import services
from .configuration import ArchiveConfigurationError, operational_archive_roots
from .models import ArchivedFile

_MESSAGES = {
    "NOT_ELIGIBLE": "Račun še ni upravičen do arhiviranja. Potrebna sta poslan samostojni ST in veljavno dokazilo dostave.",
    "READY": "Račun je pripravljen za arhiviranje.",
    "IN_PROGRESS": "Arhiviranje je v teku oziroma nepopolno. Izrecno ponovite arhiviranje.",
    "BOTH_MISSING": "Arhiviranje je nepopolno. Obe arhivski kopiji manjkata.",
    "LOCAL_MISSING": "Lokalna arhivska kopija manjka. Arhiviranje je nepopolno.",
    "ICLOUD_MISSING": "Kopija v arhivu iCloud manjka. Arhiviranje je nepopolno.",
    "LOCAL_CHECKSUM_MISMATCH": "Kontrolna vsota lokalne kopije se ne ujema. Kopije ne prepisujemo; potreben je pregled.",
    "ICLOUD_CHECKSUM_MISMATCH": "Kontrolna vsota kopije iCloud se ne ujema. Kopije ne prepisujemo; potreben je pregled.",
    "METADATA_CONFLICT": "Konflikt metapodatkov ali neveljavne poti arhiva. Preverite nastavitve in shranjeni zapis; samodejni popravek ni dovoljen.",
    "SOURCE_INVALID": "Celovitost registriranega uradnega PDF ni potrjena. Arhiviranje je blokirano; potreben je pregled dokumenta in dokazila dostave.",
    "FAILED": "Arhiviranje je nepopolno ali preverjanje ni uspelo. Preverite dostopnost obeh lokacij in ponovite dejanje.",
    "VERIFIED": "Obe arhivski kopiji sta prisotni in preverjeni s SHA-256.",
}


@dataclass(frozen=True, slots=True)
class STArchiveStatus:
    state: str
    message: str
    can_archive: bool = False
    retry: bool = False

    @property
    def verified(self):
        return self.state == services.ArchiveState.VERIFIED


def _status(state, *, can_archive=False, retry=False):
    return STArchiveStatus(state, _MESSAGES[state], can_archive, retry)


def _invoice(invoice, company_id):
    if invoice.pk is None or invoice._state.adding:
        return None
    return Invoice.objects.filter(
        pk=invoice.pk, company_id=company_id, document_type=Invoice.DocumentType.ST,
        lease__isnull=True, povezan_racun__isnull=True,
        status=Invoice.Status.POSLAN,
    ).exclude(stevilka__isnull=True).exclude(stevilka="").first()


def _configured_roots():
    return operational_archive_roots()


def _inspect(invoice, company_id):
    if connection.in_atomic_block or not connection.get_autocommit():
        return None, None, _status("FAILED")
    current = _invoice(invoice, company_id)
    if current is None or not has_sent_delivery_evidence(current):
        return None, None, _status("NOT_ELIGIBLE")
    if current.rendered_eslog_path or current.rendered_eslog_checksum:
        return None, None, _status("SOURCE_INVALID")
    if stanje_koncnega_pdf(current)["state"] != "READY":
        return None, None, _status("SOURCE_INVALID")
    roots = _configured_roots()
    paths = services.archive_paths(current, roots)
    archived = ArchivedFile.objects.filter(
        invoice=current, artifact_type=ArchivedFile.ArtifactType.PDF,
    ).first()
    if archived is None:
        return current, roots, _status("READY", can_archive=True)
    if archived.status not in ArchivedFile.Status.values or (
        archived.local_path, archived.icloud_path, archived.checksum
    ) != (
        str(paths[0]), str(paths[1]), current.checksum,
    ):
        return None, None, _status("METADATA_CONFLICT")
    verification = services.verify_archived_file(archived, roots=roots)
    state = verification.state
    if state == services.ArchiveState.BOTH_MISSING and archived.status == ArchivedFile.Status.IN_PROGRESS:
        state = "IN_PROGRESS"
    can_archive = state in {"IN_PROGRESS", "BOTH_MISSING", "LOCAL_MISSING", "ICLOUD_MISSING", "FAILED"}
    # Identical live copies may still need the short metadata commit after interruption.
    if verification.success and archived.status != ArchivedFile.Status.VERIFIED:
        return current, roots, _status("IN_PROGRESS", can_archive=True, retry=True)
    return current, roots, _status(state, can_archive=can_archive, retry=can_archive)


def st_archive_status(invoice, *, company_id):
    """Read current evidence and live copies; never create or repair anything."""
    try:
        return _inspect(invoice, company_id)[2]
    except (ArchiveConfigurationError, services.ArchiveConflict, ValueError, RuntimeError):
        return _status("METADATA_CONFLICT")
    except (OSError, DatabaseError, ObjectDoesNotExist):
        return _status("FAILED")


def archive_st_invoice(invoice, *, company_id):
    """Explicit archive/retry, with safe feedback and no delivery or PDF mutation."""
    try:
        current, roots, state = _inspect(invoice, company_id)
        if not state.can_archive:
            return state
        result = services.archive_pdf(current, roots)
        return _status(
            result.state,
            can_archive=not result.success and result.state not in {
                services.ArchiveState.METADATA_CONFLICT,
                services.ArchiveState.LOCAL_CHECKSUM_MISMATCH,
                services.ArchiveState.ICLOUD_CHECKSUM_MISMATCH,
            },
            retry=not result.success,
        )
    except (ArchiveConfigurationError, services.ArchiveConflict, ValueError, RuntimeError):
        return _status("METADATA_CONFLICT")
    except (OSError, DatabaseError, ObjectDoesNotExist):
        return _status("FAILED")
