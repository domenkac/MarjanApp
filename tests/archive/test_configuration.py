from pathlib import Path

import pytest
from django.db import IntegrityError, transaction

from archive.configuration import (
    ArchiveConfigurationError,
    resolve_archive_roots,
    verify_and_save_archive_roots,
)
from archive.models import ArchiveConfiguration
from archive.services import ArchiveRoots

pytestmark = pytest.mark.django_db(transaction=True)


def test_no_manual_archive_paths_are_hardcoded():
    project_root = Path(__file__).parents[2]
    forbidden = (
        "C:" + "\\MarjanArhiv\\Local",
        "C:" + "\\MarjanArhiv\\iCloudTest",
    )
    sources = [
        *project_root.joinpath("archive").rglob("*.py"),
        *project_root.joinpath("ui").rglob("*.py"),
        *project_root.joinpath("ui", "templates").rglob("*.html"),
    ]
    assert all(
        value not in path.read_text(encoding="utf-8")
        for path in sources
        for value in forbidden
    )


def test_configuration_requires_absolute_distinct_roots(tmp_path):
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots("relative", tmp_path)
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(tmp_path, tmp_path)
    assert not ArchiveConfiguration.objects.exists()


def test_verification_cleans_probes_and_saves_only_after_both_pass(tmp_path):
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()

    configuration = verify_and_save_archive_roots(local, icloud)

    assert configuration.verified_at is not None
    assert configuration.local_root == str(local.resolve())
    assert configuration.icloud_root == str(icloud.resolve())
    assert list(local.iterdir()) == []
    assert list(icloud.iterdir()) == []


def test_failed_icloud_preserves_previous_configuration_and_cleans_local(tmp_path):
    old_local = tmp_path / "old-local"
    old_icloud = tmp_path / "old-icloud"
    new_local = tmp_path / "new-local"
    missing_icloud = tmp_path / "missing"
    for path in (old_local, old_icloud, new_local):
        path.mkdir()
    original = verify_and_save_archive_roots(old_local, old_icloud)

    with pytest.raises(ArchiveConfigurationError, match="iCloud arhiv"):
        verify_and_save_archive_roots(new_local, missing_icloud)

    original.refresh_from_db()
    assert original.local_root == str(old_local.resolve())
    assert original.icloud_root == str(old_icloud.resolve())
    assert list(new_local.iterdir()) == []


def test_resolver_returns_archive_roots_and_missing_fails_closed(tmp_path):
    with pytest.raises(ArchiveConfigurationError):
        resolve_archive_roots()
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()
    verify_and_save_archive_roots(local, icloud)
    assert resolve_archive_roots() == ArchiveRoots(local, icloud)


def test_verification_refuses_database_transaction(tmp_path):
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()
    with transaction.atomic(), pytest.raises(
        ArchiveConfigurationError, match="transakciji"
    ):
        verify_and_save_archive_roots(local, icloud)


def test_database_enforces_singleton():
    ArchiveConfiguration.objects.create(
        local_root=str(Path("C:/one")),
        icloud_root=str(Path("C:/two")),
        verified_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(IntegrityError):
        ArchiveConfiguration.objects.bulk_create(
            [
                ArchiveConfiguration(
                    id=2,
                    local_root=str(Path("C:/three")),
                    icloud_root=str(Path("C:/four")),
                    verified_at="2026-01-01T00:00:00Z",
                )
            ]
        )
