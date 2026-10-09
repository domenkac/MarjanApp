import pytest

from archive.models import ArchiveConfiguration
from archive.services import ArchiveRoots
from invoicing.models import MonthlyCycle

pytestmark = pytest.mark.django_db(transaction=True)


def test_get_does_not_touch_filesystem(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not verify filesystem")

    monkeypatch.setattr("archive.configuration._verify_root", forbidden)
    response = client.get("/nastavitve/arhiv/")
    assert response.status_code == 200
    assert "Preveri in shrani" in response.content.decode()


def test_post_verifies_all_three_roots_and_shows_timestamp(client, tmp_path):
    local = tmp_path / "local"
    icloud = tmp_path / "icloud"
    local.mkdir()
    icloud.mkdir()
    dropbox = tmp_path / "dropbox"
    dropbox.mkdir()
    response = client.post(
        "/nastavitve/arhiv/",
        {"local_root": local, "icloud_root": icloud, "dropbox_root": dropbox},
    )
    assert response.status_code == 200
    assert "Vse tri lokacije so preverjene in shranjene." in response.content.decode()
    assert ArchiveConfiguration.objects.get().verified_at is not None
    assert list(local.iterdir()) == []
    assert list(icloud.iterdir()) == []


def test_post_is_csrf_protected():
    from django.test import Client

    response = Client(enforce_csrf_checks=True).post("/nastavitve/arhiv/", {})
    assert response.status_code == 403


def test_os_error_is_sanitized(client, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    secret = tmp_path / "secret-location"
    response = client.post(
        "/nastavitve/arhiv/",
        {
            "local_root": local,
            "icloud_root": secret,
            "dropbox_root": tmp_path / "dropbox",
        },
    )
    content = response.content.decode()
    assert "iCloud arhiv ni dostopen" in content
    assert "Traceback" not in content
    assert "OSError" not in content


def _empty_archive_state(cycle):
    return {
        "cycle_id": cycle.pk,
        "complete": True,
        "selected_count": 0,
        "verified_count": 0,
        "pending_count": 0,
        "issue_count": 0,
        "per_invoice": [],
    }


def test_step6_get_reads_state_without_archiving(client, monkeypatch, tmp_path):
    cycle = MonthlyCycle.objects.create(
        obdobje="2026-10-01",
        datum_izdaje="2026-10-01",
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    calls = []
    monkeypatch.setattr(
        "ui.views.stanje_arhiviranja_mesecnega_cikla",
        lambda received: calls.append(received.pk) or _empty_archive_state(cycle),
    )
    monkeypatch.setattr(
        "ui.views.resolve_archive_roots",
        lambda: ArchiveRoots(tmp_path / "local", tmp_path / "icloud"),
    )
    monkeypatch.setattr(
        "ui.views.arhiviraj_poslane_racune_mesecnega_cikla",
        lambda *_args: pytest.fail("GET must not archive"),
    )

    response = client.get("/mesecni-obracun/")

    assert response.status_code == 200
    assert calls == [cycle.pk]
    assert "Arhiviranje" in response.content.decode()


def test_step6_without_configuration_does_not_read_archive_state(client, monkeypatch):
    MonthlyCycle.objects.create(
        obdobje="2026-10-01",
        datum_izdaje="2026-10-01",
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    monkeypatch.setattr(
        "ui.views.stanje_arhiviranja_mesecnega_cikla",
        lambda *_args: pytest.fail("Missing configuration must skip archive state"),
    )

    response = client.get("/mesecni-obracun/")

    assert response.status_code == 200
    assert "Lokacije arhiva niso konfigurirane" in response.content.decode()


def test_archive_post_outside_step6_cannot_archive(client, monkeypatch):
    MonthlyCycle.objects.create(
        obdobje="2026-10-01",
        datum_izdaje="2026-10-01",
        trenutni_korak=MonthlyCycle.Korak.PAKETNI_PREGLED,
    )
    monkeypatch.setattr(
        "ui.views.arhiviraj_poslane_racune_mesecnega_cikla",
        lambda *_args: pytest.fail("Wrong step must not archive"),
    )
    response = client.post("/mesecni-obracun/", {"action": "archive_sent_invoices"})
    assert response.status_code == 200
    assert "Arhiviranje je dovoljeno samo v koraku 6." in response.content.decode()


def test_archive_post_without_configuration_shows_archive_error(client):
    MonthlyCycle.objects.create(
        obdobje="2026-10-01",
        datum_izdaje="2026-10-01",
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )

    response = client.post("/mesecni-obracun/", {"action": "archive_sent_invoices"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "Lokacije arhiva niso konfigurirane." in content
    assert "Neveljaven kandidat." not in content


def test_archive_post_uses_monthly_orchestrator(client, monkeypatch, tmp_path):
    cycle = MonthlyCycle.objects.create(
        obdobje="2026-10-01",
        datum_izdaje="2026-10-01",
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    roots = ArchiveRoots(tmp_path / "local", tmp_path / "icloud")
    calls = []
    monkeypatch.setattr("ui.views.resolve_archive_roots", lambda: roots)
    monkeypatch.setattr(
        "ui.views.arhiviraj_poslane_racune_mesecnega_cikla",
        lambda received, received_roots: calls.append((received.pk, received_roots)),
    )
    monkeypatch.setattr(
        "archive.services.archive_pdf",
        lambda *_args: pytest.fail("UI must not call archive_pdf directly"),
    )

    response = client.post("/mesecni-obracun/", {"action": "archive_sent_invoices"})

    assert response.status_code == 302
    assert calls == [(cycle.pk, roots)]


@pytest.mark.parametrize(
    "parent,child", [(0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)]
)
def test_settings_form_rejects_nested_archive_locations(
    client, tmp_path, parent, child
):
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    roots[child] = roots[parent] / "nested"
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/nastavitve/arhiv/",
        dict(zip(("local_root", "icloud_root", "dropbox_root"), roots)),
    )
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not ArchiveConfiguration.objects.exists()


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("media_is_parent", [False, True])
def test_settings_form_rejects_media_overlap(
    client, tmp_path, settings, index, media_is_parent
):
    roots = [tmp_path / name for name in ("local", "icloud", "dropbox")]
    settings.MEDIA_ROOT = (
        roots[index].parent if media_is_parent else roots[index] / "media"
    )
    for root in roots:
        root.mkdir()
    response = client.post(
        "/nastavitve/arhiv/",
        dict(zip(("local_root", "icloud_root", "dropbox_root"), roots)),
    )
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not ArchiveConfiguration.objects.exists()
