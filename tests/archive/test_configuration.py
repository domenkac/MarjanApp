from pathlib import Path

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from archive.configuration import (
    ArchiveConfigurationError,
    resolve_archive_locations,
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
        verify_and_save_archive_roots("relative", tmp_path, tmp_path / "dropbox")
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(tmp_path, tmp_path, tmp_path / "dropbox")
    assert not ArchiveConfiguration.objects.exists()


def test_verification_cleans_probes_and_saves_only_after_all_three_pass(tmp_path):
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()
    dropbox = tmp_path / "dropbox"
    dropbox.mkdir()

    configuration = verify_and_save_archive_roots(local, icloud, dropbox)

    assert configuration.verified_at is not None
    assert configuration.local_root == str(local.resolve())
    assert configuration.icloud_root == str(icloud.resolve())
    assert list(local.iterdir()) == []
    assert list(icloud.iterdir()) == []
    assert list(dropbox.iterdir()) == []
    assert configuration.dropbox_root == str(dropbox.resolve())


def test_failed_icloud_preserves_previous_configuration_and_cleans_local(tmp_path):
    old_local = tmp_path / "old-local"
    old_icloud = tmp_path / "old-icloud"
    new_local = tmp_path / "new-local"
    missing_icloud = tmp_path / "missing"
    for path in (old_local, old_icloud, new_local):
        path.mkdir()
    dropbox = tmp_path / "dropbox"
    dropbox.mkdir()
    original = verify_and_save_archive_roots(old_local, old_icloud, dropbox)

    with pytest.raises(ArchiveConfigurationError, match="iCloud arhiv"):
        verify_and_save_archive_roots(new_local, missing_icloud, dropbox)

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
    dropbox = tmp_path / "dropbox"
    dropbox.mkdir()
    verify_and_save_archive_roots(local, icloud, dropbox)
    assert resolve_archive_roots() == ArchiveRoots(local, icloud)


def test_verification_refuses_database_transaction(tmp_path):
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()
    dropbox = tmp_path / "dropbox"
    dropbox.mkdir()
    with (
        transaction.atomic(),
        pytest.raises(ArchiveConfigurationError, match="transakciji"),
    ):
        verify_and_save_archive_roots(local, icloud, dropbox)


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


@pytest.mark.parametrize(
    "parent,child", [(0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)]
)
def test_nested_roots_rejected_before_probe_and_by_persisted_config(
    tmp_path, settings, monkeypatch, parent, child
):
    settings.MEDIA_ROOT = tmp_path / "missing-media"
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    roots[child] = roots[parent] / "nested"
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)

    def forbidden(*args):
        raise AssertionError("Overlapping paths must fail before filesystem probes")

    monkeypatch.setattr("archive.configuration._verify_root", forbidden)
    with pytest.raises(ArchiveConfigurationError):
        verify_and_save_archive_roots(*roots)
    assert not ArchiveConfiguration.objects.exists()
    ArchiveConfiguration.objects.create(
        local_root=str(roots[0]),
        icloud_root=str(roots[1]),
        dropbox_root=str(roots[2]),
        verified_at=timezone.now(),
    )
    with pytest.raises(ArchiveConfigurationError):
        resolve_archive_locations()


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("media_is_parent", [False, True])
def test_media_overlap_rejected_even_when_media_is_missing(
    tmp_path, settings, index, media_is_parent
):
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    if media_is_parent:
        settings.MEDIA_ROOT = roots[index]
        roots[index] = roots[index] / "archive"
    else:
        settings.MEDIA_ROOT = roots[index] / "missing-media"
    # Path comparison must not depend on the directories existing.
    from archive.configuration import validate_archive_locations

    with pytest.raises(ArchiveConfigurationError):
        validate_archive_locations(*roots)
    ArchiveConfiguration.objects.create(
        local_root=str(roots[0]),
        icloud_root=str(roots[1]),
        dropbox_root=str(roots[2]),
        verified_at=timezone.now(),
    )
    with pytest.raises(ArchiveConfigurationError):
        resolve_archive_locations()


def test_nonexistent_disjoint_media_does_not_block_configuration(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "missing-media"
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    for root in roots:
        root.mkdir()
    assert verify_and_save_archive_roots(*roots).verified_at is not None
    assert not settings.MEDIA_ROOT.exists()


def test_overlap_comparison_resolves_dotdot_paths(tmp_path, settings):
    from archive.configuration import validate_archive_locations

    settings.MEDIA_ROOT = tmp_path / "media"
    with pytest.raises(ArchiveConfigurationError):
        validate_archive_locations(
            tmp_path / "local",
            tmp_path / "icloud",
            tmp_path / "unused" / ".." / "local" / "dropbox",
        )
