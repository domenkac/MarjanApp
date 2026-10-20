import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from core.services import (
    audit_field_changes,
    financial_changes,
    record_edit_fingerprint,
    validate_edit_source,
)

from .models import ArchiveConfiguration

_PROBE_BYTES = b"MarjanApp archive location verification\n"


class ArchiveConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveLocations:
    local_root: Path
    icloud_root: Path
    dropbox_root: Path

    def __post_init__(self):
        paths = [
            Path(value)
            for value in (self.local_root, self.icloud_root, self.dropbox_root)
        ]
        if not all(path.is_absolute() for path in paths):
            raise ValueError("Vse poti morajo biti absolutne.")
        paths = [path.resolve() for path in paths]
        if len(set(paths)) != 3:
            raise ValueError("Vse tri lokacije morajo biti različne.")
        for name, path in zip(("local_root", "icloud_root", "dropbox_root"), paths):
            object.__setattr__(self, name, path)


def _roots(local_root, icloud_root):
    from .services import ArchiveRoots

    if not str(local_root).strip() or not str(icloud_root).strip():
        raise ArchiveConfigurationError("Obe poti arhiva sta obvezni.")
    try:
        return ArchiveRoots(Path(local_root), Path(icloud_root))
    except ValueError as error:
        raise ArchiveConfigurationError(str(error)) from None


def resolve_archive_roots():
    configuration = ArchiveConfiguration.objects.filter(pk=1).first()
    if configuration is None:
        raise ArchiveConfigurationError("Lokacije arhiva niso konfigurirane.")
    return _roots(configuration.local_root, configuration.icloud_root)


def operational_archive_roots():
    """Validate the saved operational paths without creating or probing files."""
    roots = resolve_archive_roots()
    configuration = ArchiveConfiguration.objects.get(pk=1)
    paths = [roots.local_root, roots.icloud_root]
    for saved, resolved in zip(
        (configuration.local_root, configuration.icloud_root), paths
    ):
        if Path(saved) != resolved:
            raise ArchiveConfigurationError("Configured archive root was redirected.")
    for value in (settings.MEDIA_ROOT, settings.RENDERING_ROOT, configuration.dropbox_root):
        if value:
            path = Path(value)
            if not path.is_absolute():
                raise ArchiveConfigurationError("Invalid configured path.")
            paths.append(path.resolve())
    for index, path in enumerate(paths[:2]):
        for other in paths[index + 1:]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ArchiveConfigurationError("Archive locations overlap.")
    database = Path(connection.settings_dict["NAME"]).resolve()
    if any(database.is_relative_to(root) or root.is_relative_to(database) for root in paths[:2]):
        raise ArchiveConfigurationError("Archive location overlaps the database.")
    return roots


def resolve_archive_locations() -> ArchiveLocations:
    configuration = ArchiveConfiguration.objects.filter(pk=1).first()
    if configuration is None:
        raise ArchiveConfigurationError("Lokacije arhiva niso konfigurirane.")
    return validate_archive_locations(
        configuration.local_root, configuration.icloud_root, configuration.dropbox_root
    )


def validate_archive_locations(local_root, icloud_root, dropbox_root):
    if getattr(settings, "MARJAN_DESKTOP_TEST", False):
        from desktop.profile import DesktopError, require_test_archive_paths

        try:
            require_test_archive_paths((local_root, icloud_root, dropbox_root))
        except (ValueError, DesktopError):
            raise ArchiveConfigurationError(
                "TEST dovoljuje samo zasebne sintetične lokacije arhiva."
            ) from None
    if not all(str(value).strip() for value in (local_root, icloud_root, dropbox_root)):
        raise ArchiveConfigurationError("Vse tri poti so obvezne.")
    try:
        roots = ArchiveLocations(local_root, icloud_root, dropbox_root)
        media = Path(settings.MEDIA_ROOT)
        if not media.is_absolute():
            raise ValueError("MEDIA_ROOT must be absolute.")
        # Safety comparison must also work before MEDIA_ROOT exists.
        paths = [
            roots.local_root,
            roots.icloud_root,
            roots.dropbox_root,
            media.resolve(),
        ]
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError("Backup locations must not overlap.")
        return roots
    except (ValueError, OSError, RuntimeError):
        raise ArchiveConfigurationError(
            "Poti morajo biti veljavne, absolutne in se ne smejo prekrivati."
        ) from None


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
        temporary.unlink()
        temporary = None
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


def verify_and_save_archive_roots(
    local_root, icloud_root, dropbox_root, *, razlog="", source_fingerprint=None,
) -> ArchiveConfiguration:
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ArchiveConfigurationError(
            "Preverjanje lokacij ne sme potekati v transakciji baze."
        )
    roots = validate_archive_locations(local_root, icloud_root, dropbox_root)
    original = ArchiveConfiguration.objects.filter(pk=1).first()
    if source_fingerprint is None:
        source_fingerprint = record_edit_fingerprint(original) if original else "new"
    _verify_root(roots.local_root, "Lokalni arhiv")
    _verify_root(roots.icloud_root, "iCloud arhiv")
    _verify_root(roots.dropbox_root, "Dropbox mapa")
    with transaction.atomic():
        previous = ArchiveConfiguration.objects.select_for_update().filter(pk=1).first()
        configuration = ArchiveConfiguration(
            pk=1, local_root=str(roots.local_root), icloud_root=str(roots.icloud_root),
            dropbox_root=str(roots.dropbox_root), verified_at=timezone.now(),
            verification_evidence={
                "local_root": str(roots.local_root), "icloud_root": str(roots.icloud_root),
                "dropbox_root": str(roots.dropbox_root), "operations": ["write", "read", "delete"],
            },
        )
        changes = {}
        if previous is not None:
            validate_edit_source(previous, source_fingerprint)
            changes = financial_changes(
                previous, configuration, ("local_root", "icloud_root", "dropbox_root"), razlog,
            )
            configuration.created_at = previous.created_at
        elif source_fingerprint not in ("", "new"):
            raise ValidationError("Zapis ne obstaja več. Osvežite obrazec.")
        configuration.save(force_insert=previous is None, force_update=previous is not None)
        audit_field_changes(configuration, changes, razlog)
    return configuration
