from datetime import date

import pytest
from django.utils import timezone

from companies.models import Company
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from invoicing.services import CALCULATION_SNAPSHOT_VERSION


def ustvari_podjetje(naziv, davcna_stevilka, *, aktivno=True):
    return Company.objects.create(
        drzava_koda="SI",
        naziv=naziv,
        davcna_stevilka=davcna_stevilka,
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja=f"marjanapp-{davcna_stevilka}",
        aktivno=aktivno,
    )


def _mixed_schema_cycle():
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2031, 2, 1),
        datum_izdaje=date(2031, 2, 12),
        trenutni_korak=MonthlyCycle.Korak.IZRACUN_OSNUTKOV,
    )
    current = MonthlyBatch.objects.create(
        cycle=cycle,
        company=ustvari_podjetje("Alfa d.o.o.", "15012557"),
        preflight_status=MonthlyBatch.PreflightStatus.PASSED,
        calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
        calculation_schema_version=CALCULATION_SNAPSHOT_VERSION,
        calculation_snapshot={"schema_version": CALCULATION_SNAPSHOT_VERSION},
        calculated_at=timezone.now(),
    )
    stale = MonthlyBatch.objects.create(
        cycle=cycle,
        company=ustvari_podjetje("Beta d.o.o.", "69165028"),
        preflight_status=MonthlyBatch.PreflightStatus.PASSED,
        calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
        calculation_schema_version=2,
        calculation_snapshot={"schema_version": 2},
        calculated_at=timezone.now(),
    )
    return cycle, current, stale


@pytest.mark.django_db
def test_stran_prikaze_aktiven_prvi_korak_in_vseh_sest_oznak(client):
    response = client.get("/mesecni-obracun/")
    vsebina = response.content.decode()

    assert response.status_code == 200
    assert 'class="page-heading billing-heading"' in vsebina
    assert "dashboard-heading" not in vsebina
    assert 'class="nav-item is-active" href="/" aria-current="page"' in vsebina
    assert 'class="wizard-step is-active" aria-current="step"' in vsebina
    for oznaka in (
        "Izbira meseca",
        "Predhodna kontrola",
        "Izračun osnutkov",
        "Paketni pregled Δ",
        "Potrditev paketa",
        "Zaključek ciklusa",
    ):
        assert oznaka in vsebina
    assert "eSLOG" not in vsebina
    assert "XML" not in vsebina


@pytest.mark.django_db
def test_get_privzeto_uporabi_tekoci_mesec_in_prvi_dan_za_izdajo(client):
    danes = timezone.localdate()

    response = client.get("/mesecni-obracun/")

    assert response.context["form"].initial == {
        "leto": danes.year,
        "mesec": danes.month,
        "datum_izdaje": danes.replace(day=1),
    }
    assert response.context["obdobje_od"] == danes.replace(day=1)
    assert f'value="{danes:%Y-%m}-01"' in response.content.decode()


@pytest.mark.django_db
def test_prikaze_samo_resnicna_aktivna_podjetja(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    ustvari_podjetje("Neaktivno podjetje d.o.o.", "69165028", aktivno=False)

    vsebina = client.get("/mesecni-obracun/").content.decode()

    assert "Aktivno podjetje d.o.o." in vsebina
    assert "Neaktivno podjetje d.o.o." not in vsebina
    assert "Alpha Real Estate" not in vsebina
    assert "Beta Properties" not in vsebina
    assert "Aktivna podjetja" in vsebina
    assert "Predhodna kontrola se izvaja na ravni posameznega podjetja." in vsebina
    assert "Vključena so vsa aktivna podjetja." not in vsebina
    assert "obračun vseh aktivnih izdajateljev" not in vsebina


@pytest.mark.django_db
def test_brez_aktivnih_podjetij_prikaze_blokado(client):
    response = client.get("/mesecni-obracun/")

    assert "Ni aktivnih podjetij" in response.content.decode()
    assert b"disabled" in response.content


@pytest.mark.django_db
def test_neposreden_post_brez_podjetij_prikaze_storitveno_napako(client):
    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )

    assert response.status_code == 200
    assert "ni mogoče začeti brez aktivnega podjetja" in response.content.decode()
    assert MonthlyCycle.objects.count() == 0
    assert MonthlyBatch.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("podatki", "napaka"),
    [
        (
            {"leto": "2026", "mesec": "13", "datum_izdaje": "2026-08-01"},
            "Izberite veljaven obračunski mesec.",
        ),
        (
            {"leto": "0", "mesec": "8", "datum_izdaje": "2026-08-01"},
            "Vnesite leto, ki ga koledar podpira.",
        ),
        (
            {"leto": "abc", "mesec": "8", "datum_izdaje": "2026-08-01"},
            "Vnesite veljavno obračunsko leto.",
        ),
    ],
)
def test_neveljaven_mesec_ali_leto_je_zavrnjen(client, podatki, napaka):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post("/mesecni-obracun/", podatki)

    assert response.status_code == 200
    assert napaka in response.content.decode()
    assert response.context["nadaljevanje"] is None


@pytest.mark.django_db
def test_neveljaven_datum_izdaje_ohrani_obdobje_iz_oddanega_meseca(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "ni-datum"},
    )

    assert response.context["nadaljevanje"] is None
    assert response.context["obdobje_od"] == date(2031, 2, 1)
    assert response.context["obdobje_do"] == date(2031, 2, 28)
    assert "01. 02. 2031 – 28. 02. 2031" in response.content.decode()


@pytest.mark.django_db
def test_veljavna_izbira_uporabi_prg_in_ne_ustvari_racuna(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2026", "mesec": "8", "datum_izdaje": "2026-08-12"},
    )

    assert response.status_code == 302
    assert response.url == "/mesecni-obracun/"
    cycle = MonthlyCycle.objects.get()
    assert cycle.trenutni_korak == 2
    assert MonthlyBatch.objects.count() == 1
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("datum_izdaje", ["2024-02-01", "2024-03-12"])
def test_prestopni_februar_ostane_obdobje_storitve_ne_glede_na_datum_izdaje(
    client,
    datum_izdaje,
):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2024", "mesec": "2", "datum_izdaje": datum_izdaje},
    )

    assert response.status_code == 302
    assert MonthlyCycle.objects.get().obdobje == date(2024, 2, 1)


@pytest.mark.django_db
def test_nov_brskalnik_obnovi_mesec_datum_in_drugi_korak(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )
    assert response.status_code == 302

    from django.test import Client

    nov_brskalnik = Client()
    response = nov_brskalnik.get("/mesecni-obracun/")
    vsebina = response.content.decode()

    assert response.context["trenutni_korak"] == 2
    assert response.context["nadaljevanje"] == {
        "leto": 2031,
        "mesec": 2,
        "datum_izdaje": date(2031, 2, 12),
    }
    assert "02/2031" in vsebina
    assert "12. 02. 2031" in vsebina
    assert 'class="wizard-step is-completed"' in vsebina
    assert 'class="wizard-step is-active" aria-current="step"><span>2</span>' in vsebina
    assert "eSLOG" not in vsebina
    assert "XML" not in vsebina
    assert "sessionid" not in nov_brskalnik.cookies


@pytest.mark.django_db
def test_konflikten_post_na_drugem_koraku_prikaze_napako_brez_obrazca(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-13"},
    )
    vsebina = response.content.decode()

    assert response.status_code == 200
    assert "Datum izdaje se ne ujema" in vsebina
    assert 'role="alert"' in vsebina
    assert "<form" not in vsebina
    assert MonthlyCycle.objects.get().datum_izdaje == date(2031, 2, 12)


@pytest.mark.django_db
def test_step2_prikaze_trajne_rezultate_in_brez_starega_placeholderja(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )

    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    batch = MonthlyBatch.objects.get()

    assert batch.preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    assert batch.preflight_checked_at is not None
    assert "DDV_POTRDITEV_MANJKA" not in content
    assert "Potrditev nastavitev DDV manjka." in content
    assert "Predhodna kontrola še ni implementirana." not in content
    assert "eSLOG" not in content
    assert "XML" not in content


@pytest.mark.django_db
def test_step2_brez_uspesnega_podjetja_ne_nadaljuje(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )

    response = client.post(
        "/mesecni-obracun/", {"action": "continue_step3"}, follow=True
    )

    assert response.redirect_chain == [("/mesecni-obracun/", 302)]
    assert MonthlyCycle.objects.get().trenutni_korak == 2


@pytest.mark.django_db
def test_vsa_uspesna_podjetja_nadaljujejo_na_step3_in_oznacijo_step2_zeleno(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )
    MonthlyBatch.objects.update(preflight_status=MonthlyBatch.PreflightStatus.PASSED)

    response = client.post(
        "/mesecni-obracun/", {"action": "continue_step3"}, follow=True
    )
    content = response.content.decode()

    assert MonthlyCycle.objects.get().trenutni_korak == 3
    assert "Izračun osnutkov" in content
    assert "Računi še niso ustvarjeni." in content
    assert "wizard-step is-completed" in content
    assert "eSLOG" not in content
    assert "XML" not in content


@pytest.mark.django_db
def test_delni_step2_je_attention_in_se_lahko_ponovi_brez_pomika_nazaj(client):
    ustvari_podjetje("Alfa d.o.o.", "15012557")
    ustvari_podjetje("Beta d.o.o.", "69165028")
    client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "2031-02-12"},
    )
    first_batch = MonthlyBatch.objects.order_by("pk").first()
    MonthlyBatch.objects.filter(pk=first_batch.pk).update(
        preflight_status=MonthlyBatch.PreflightStatus.PASSED
    )
    client.post("/mesecni-obracun/", {"action": "continue_step3"})

    response = client.get("/mesecni-obracun/")
    assert "wizard-step is-attention" in response.content.decode()

    response = client.post(
        "/mesecni-obracun/", {"action": "rerun_preflight"}, follow=True
    )
    assert response.redirect_chain == [("/mesecni-obracun/", 302)]
    assert MonthlyCycle.objects.get().trenutni_korak == 3


@pytest.mark.django_db
def test_mixed_schema_ne_more_nadaljevati_v_step4(client):
    cycle, _, _ = _mixed_schema_cycle()

    client.post("/mesecni-obracun/", {"action": "continue_step4"})

    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.IZRACUN_OSNUTKOV


@pytest.mark.django_db
def test_mixed_schema_step3_context_ni_dokoncan(client):
    _, current, stale = _mixed_schema_cycle()

    response = client.get("/mesecni-obracun/")
    batches = {batch.pk: batch for batch in response.context["batches"]}

    assert response.context["step3_complete"] is False
    assert response.context["step3_can_continue"] is False
    assert batches[current.pk].is_active_calculation is True
    assert batches[stale.pk].is_active_calculation is False


@pytest.mark.django_db
def test_stara_shema_zahteva_ponovni_izracun_brez_veljavnega_casa(client):
    cycle, current, stale = _mixed_schema_cycle()
    current.delete()

    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    batch = response.context["batches"][0]

    assert cycle.trenutni_korak == MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    assert batch.pk == stale.pk
    assert batch.is_active_calculation is False
    assert batch.needs_recalculation is True
    assert "Potreben ponovni izračun" in content
    assert "Izračunano " not in content
    assert 'class="status-badge">Potreben ponovni izračun</span>' in content
