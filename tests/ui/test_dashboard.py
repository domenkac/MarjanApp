from datetime import UTC, date, datetime

import pytest
from django.template.loader import render_to_string

from companies.models import Company
from invoicing.models import Invoice, MonthlyCycle


@pytest.mark.django_db
def test_prazna_nadzorna_plosca(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.context["counts"] == {
        "podjetja": 0,
        "najemniki": 0,
        "pogodbe": 0,
        "izdani_racuni": 0,
    }
    vsebina = response.content.decode()
    assert "<h1>Nadzorna plošča</h1>" in vsebina
    assert 'class="page-heading dashboard-heading"' in vsebina
    assert 'aria-current="page"' in vsebina
    assert 'href="/mesecni-obracun/"' in vsebina
    assert "Začni mesečni obračun" in vsebina
    assert "Mesečni obračun in ročne fakture še niso na voljo." not in vsebina
    assert 'class="nav-item is-active" href="/" aria-current="page"' in vsebina
    assert "eSLOG" not in vsebina
    assert "XML" not in vsebina
    assert "Dostava je del mesečnega obračuna" in vsebina
    assert "Stanje dostave je prikazano v aktivnem mesečnem postopku." in vsebina
    assert "Ni še poslano" not in vsebina


def test_prva_raven_navigacije_sledi_prd():
    vsebina = render_to_string("ui/partials/sidebar.html")

    for oznaka in ("Nadzorna plošča", "Pogodbe", "Arhiv", "Nastavitve"):
        assert oznaka in vsebina
    for oznaka in (
        "Mesečni obračun",
        "Najemniki",
        "Podjetja",
        "Nepremičnine",
        "Šifranti",
        "Nova ročna faktura",
    ):
        assert oznaka not in vsebina


def test_sidebar_lahko_aktivira_drugo_prvoravensko_sekcijo():
    vsebina = render_to_string(
        "ui/partials/sidebar.html",
        {"active_section": "pogodbe"},
    )

    assert 'class="nav-item" href="/"' in vsebina
    assert (
        'class="nav-item is-active" href="/pogodbe/" aria-current="page"'
        in vsebina
    )


def test_osnovna_predloga_ne_potrebuje_dashboard_contexta():
    vsebina = render_to_string("ui/base.html")

    assert "Vsa podjetja" in vsebina
    assert "counts.podjetja" not in vsebina


@pytest.mark.django_db
def test_nadzorna_plosca_prikaze_resnicna_stevila(client):
    company = Company.objects.create(
        drzava_koda="SI",
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
    )
    Invoice.objects.create(
        company=company,
        document_type=Invoice.DocumentType.NP,
        stevilka="NP-00147",
        datum_izdaje="2026-09-21",
        obdobje_od="2026-09-01",
        obdobje_do="2026-09-30",
    )

    response = client.get("/")

    assert response.context["counts"] == {
        "podjetja": 1,
        "najemniki": 0,
        "pogodbe": 0,
        "izdani_racuni": 1,
    }
    assert "NP-00147" not in response.content.decode()


def test_nadzorna_plosca_nima_pisalnega_endpointa(client):
    assert client.post("/").status_code == 405


@pytest.mark.django_db
def test_nadzorna_plosca_ponudi_nadaljevanje_aktivnega_cikla(client):
    MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
    )

    vsebina = client.get("/").content.decode()

    assert "Nadaljuj mesečni obračun" in vsebina
    assert "Začni mesečni obračun" not in vsebina


def completed_cycle(*, corrupt=None):
    completed_at = datetime(2026, 10, 1, 12, 30, 45, tzinfo=UTC)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
        status=MonthlyCycle.Status.ZAKLJUCEN,
        completed_at=completed_at,
    )
    cycle.completion_snapshot = {
        "schema_version": 1,
        "cycle_id": cycle.pk,
        "period": "2026-09-01",
        "headline": "Mesečni postopek uspešno zaključen",
        "all_required_work_successful": True,
        "requires_attention": False,
        "prepared_count": 8,
        "sent_count": 8,
        "intentionally_unissued_count": 1,
        "failed_count": 0,
        "unknown_count": 0,
        "delivery_pending_count": 0,
        "archive_verified_count": 8,
        "archive_pending_count": 0,
        "conflict_count": 0,
        "per_company": [
            {
                "company_id": 1,
                "issuer_label": "Alfa d.o.o.",
                "prepared_count": 8,
                "sent_count": 8,
                "intentionally_unissued_count": 1,
                "failed_count": 0,
                "unknown_count": 0,
                "delivery_pending_count": 0,
                "archive_verified_count": 8,
                "archive_issue_count": 0,
                "requires_attention": False,
            }
        ],
        "issues": [],
        "summary_attempt_id": 4,
        "summary_attempt_sequence": 1,
        "summary_recipient": "owner@example.com",
        "summary_subject": "Poročilo",
        "summary_completed_at": completed_at.isoformat(),
        "summary_result": "ACCEPTED",
        "summary_result_code": "SMTP_ACCEPTED",
    }
    if corrupt == "headline":
        cycle.completion_snapshot["headline"] = "Lažen uspeh"
    elif corrupt == "top_level_sent":
        cycle.completion_snapshot["sent_count"] = 7
    elif corrupt == "malformed_company":
        cycle.completion_snapshot["per_company"] = ["invalid"]
    elif corrupt == "company_sent":
        cycle.completion_snapshot["per_company"][0]["sent_count"] = 7
    elif corrupt == "aggregate":
        cycle.completion_snapshot["per_company"][0]["intentionally_unissued_count"] = 0
    cycle.save(update_fields=["completion_snapshot"])
    return cycle


@pytest.mark.django_db
def test_dashboard_shows_persisted_latest_completed_result_in_local_time(
    client, monkeypatch
):
    cycle = completed_cycle()
    forbidden = lambda *_args, **_kwargs: pytest.fail(
        "Dashboard ne sme izvajati živih operativnih preverjanj."
    )
    for name in (
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        "stanje_arhiviranja_mesecnega_cikla",
        "stanje_dostave_mesecnega_cikla",
        "resolve_archive_roots",
        "resolve_keyring_credential_ref",
        "zakljuci_mesecni_cikel",
    ):
        monkeypatch.setattr(f"ui.views.{name}", forbidden)

    response = client.get("/")

    cycle.refresh_from_db()
    content = response.content.decode()
    assert response.status_code == 200
    assert "Začni mesečni obračun" in content
    assert "Nadaljuj mesečni obračun" not in content
    assert "Zadnji zaključen mesečni obračun" in content
    assert "09/2026" in content
    assert "01. 10. 2026 14:30:45" in content
    assert "Mesečni postopek uspešno zaključen" in content
    for value in ("8", "1", "owner@example.com", "Alfa d.o.o."):
        assert value in content
    assert cycle.status == MonthlyCycle.Status.ZAKLJUCEN


@pytest.mark.django_db
@pytest.mark.parametrize(
    "corrupt",
    ("headline", "top_level_sent", "malformed_company", "company_sent", "aggregate"),
)
def test_dashboard_does_not_trust_corrupt_completion_evidence(client, corrupt):
    completed_cycle(corrupt=corrupt)

    content = client.get("/").content.decode()

    assert "Podatkov zadnjega zaključenega mesečnega cikla ni mogoče zanesljivo prikazati." in content
    assert "Mesečni postopek uspešno zaključen" not in content
    assert "owner@example.com" not in content
