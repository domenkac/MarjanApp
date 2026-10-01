import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.db import connection
from django.utils import timezone

from .models import ArchiveConfiguration
from .services import ArchiveRoots

_PROBE_BYTES = b"MarjanApp archive location verification\n"


class ArchiveConfigurationError(ValueError):
    pass


def _roots(local_root, icloud_root):
    if not str(local_root).strip() or not str(icloud_root).strip():
        raise ArchiveConfigurationError("Obe poti arhiva sta obvezni.")
    try:
        return ArchiveRoots(Path(local_root), Path(icloud_root))
    except ValueError as error:
        raise ArchiveConfigurationError(str(error)) from None


def resolve_archive_roots() -> ArchiveRoots:
    configuration = ArchiveConfiguration.objects.filter(pk=1).first()
    if configuration is None:
        raise ArchiveConfigurationError("Lokacije arhiva niso konfigurirane.")
    return _roots(configuration.local_root, configuration.icloud_root)


def _verify_root(root: Path, label: str) -> None:
    temporary = None
    try:
        if not root.is_dir():
            raise OSError
        with NamedTemporaryFile(
            dir=root, prefix=".marjanapp-verify-", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(_PROBE_BYTES)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != _PROBE_BYTES:
            raise OSError
    except OSError:
        raise ArchiveConfigurationError(
            f"{label} ni dostopen za varen zapis in branje."
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def verify_and_save_archive_roots(local_root, icloud_root) -> ArchiveConfiguration:
    if connection.in_atomic_block:
        raise ArchiveConfigurationError(
            "Preverjanje lokacij ne sme potekati v transakciji baze."
        )
    roots = _roots(local_root, icloud_root)
    _verify_root(roots.local_root, "Lokalni arhiv")
    _verify_root(roots.icloud_root, "iCloud arhiv")
    configuration, _ = ArchiveConfiguration.objects.update_or_create(
        pk=1,
        defaults={
            "local_root": str(roots.local_root),
            "icloud_root": str(roots.icloud_root),
            "verified_at": timezone.now(),
        },
    )
    return configuration
