import re
from copy import deepcopy
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from companies.models import Company, NumberSequence
from contracts.models import Lease, Property, RecipientContact, Tenant, Unit
from delivery.credentials import KeyringCredentialRef
from delivery.models import DeliveryAttempt
from delivery.services import (
    mark_accepted,
    mark_confirmed_failure,
    mark_result_unknown,
    mark_submission_started,
)
from delivery.transport import SmtpConfig, SmtpSecurity
from invoicing.models import (
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCandidateReview,
    MonthlyCycle,
)
from invoicing.services import (
    CALCULATION_SNAPSHOT_VERSION,
    MesecniCikelNapaka,
    NeveljavenPrehodMesecnegaCikla,
    izdaj_zamrznjeni_mesecni_paket,
    odpri_zakljucek_mesecnega_cikla,
    premakni_mesecni_cikel,
    pripravi_dostavo_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.contexts import PdfRenderNapaka
from rendering.services import renderiraj_koncne_pdfje_mesecnega_paketa
from tests.invoicing.test_preflight import (
    _freeze_ready_package,
    ready_company,  # noqa: F401
)
from ui.views import _sporocilo_mesecnega_racuna


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


def _review_cycle(*candidates):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2031, 2, 1),
        datum_izdaje=date(2031, 2, 12),
        trenutni_korak=MonthlyCycle.Korak.PAKETNI_PREGLED,
    )
    reviews = []
    for index, options in enumerate(candidates, start=1):
        company = ustvari_podjetje(
            options.get("company", f"Podjetje {index} d.o.o."),
            ("15012557", "69165028", "24561004")[index - 1],
        )
        property_ = Property.objects.create(
            company=company, naziv=f"Objekt {index}", naslov="Naslov 1"
        )
        unit = Unit.objects.create(
            property=property_, oznaka=f"E-{index}", tip_enote=Unit.TipEnote.PISARNA
        )
        tenant = Tenant.objects.create(
            tip=Tenant.Tip.PRAVNA_OSEBA,
            naziv=options.get("tenant", f"Najemnik {index}"),
            davcna_stevilka="15012557",
            maticna_stevilka="1234567000",
            naslov="Trg 1",
            drzava_koda="SI",
            identificiran_za_ddv=True,
            email=f"tenant{index}@example.com",
        )
        lease = Lease.objects.create(
            unit=unit,
            tenant=tenant,
            oznaka_pogodbe=f"P-{index}",
            tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
            datum_zacetka=date(2030, 1, 1),
            rok_placila_dni=15,
        )
        fingerprint = str(index) * 64
        previous = options.get("previous", "100.00")
        exceptions = options.get("exceptions", [])
        will_issue = options.get("will_issue", True)
        candidate = {
            "lease_id": lease.pk,
            "candidate_fingerprint": fingerprint,
            "contract_reference": f"P-{index}",
            "property_label": f"Objekt {index}",
            "unit_label": f"E-{index}",
            "tenant": {"name": tenant.naziv},
            "lines": [
                {
                    "net": options.get("current", "125.00"),
                    "vat": "0.00",
                    "total": options.get("current", "125.00"),
                }
            ],
            "totals": {
                "net": options.get("current", "125.00"),
                "vat": "0.00",
                "total": options.get("current", "125.00"),
            },
            "previous_invoice": None if previous is None else {"total": previous},
            "first_issue": previous is None,
            "will_issue": will_issue,
            "non_issue_reason": options.get("system_reason", ""),
            "exceptions": [
                {"code": f"IZJEMA_{position}", "message": message}
                for position, message in enumerate(exceptions)
            ],
        }
        batch = MonthlyBatch.objects.create(
            cycle=cycle,
            company=company,
            preflight_status=MonthlyBatch.PreflightStatus.PASSED,
            calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
            calculation_schema_version=CALCULATION_SNAPSHOT_VERSION,
            calculation_snapshot={
                "schema_version": CALCULATION_SNAPSHOT_VERSION,
                "company": {"name": company.naziv},
                "candidates": [candidate],
            },
            calculated_at=timezone.now(),
        )
        reviews.append(
            MonthlyCandidateReview.objects.create(
                batch=batch,
                lease=lease,
                candidate_fingerprint=fingerprint,
                selected=will_issue,
                requires_confirmation=bool(exceptions) and will_issue,
            )
        )
    return cycle, reviews


def _step6_cycle(data, settings, tmp_path):
    RecipientContact.objects.create(
        lease=data["lease"],
        email="recipient@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=RecipientContact.Kanal.PDF,
    )
    settings.RENDERING_ROOT = tmp_path / "rendered"
    _freeze_ready_package(data)
    izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    renderiraj_koncne_pdfje_mesecnega_paketa(data["cycle"])
    return odpri_zakljucek_mesecnega_cikla(data["cycle"])


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


@pytest.mark.django_db
def test_step4_prikaze_eno_skupno_tabelo_in_povzetek_samo_trenutnih(client):
    _cycle, reviews = _review_cycle(
        {"company": "Alfa d.o.o.", "tenant": "Ana", "current": "125.00"},
        {"company": "Beta d.o.o.", "tenant": "Berta", "previous": "200.00", "current": "150.00", "exceptions": ["Sprememba."]},
        {"company": "Gama d.o.o.", "tenant": "Cene", "will_issue": False, "system_reason": "Ni postavk."},
    )
    MonthlyCandidateReview.objects.create(
        batch=reviews[0].batch, lease=reviews[0].lease,
        candidate_fingerprint="f" * 64, selected=False,
        requires_confirmation=False, non_issue_reason="Stara odločitev",
    )
    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    assert content.count('class="review-table"') == 1
    assert all(value in content for value in ("Alfa d.o.o.", "Beta d.o.o.", "Gama d.o.o."))
    assert "Stara odločitev" not in content
    assert response.context["review_summary"] == {
        "total": 3, "selected": 2, "requiring_confirmation": 1,
        "confirmed": 0, "user_deselected": 0, "system_non_issuable": 1,
    }


@pytest.mark.django_db
def test_step4_prikaze_decimalne_delte_in_posebna_izhodišča(client):
    _review_cycle(
        {"company": "Plus", "previous": "100.00", "current": "125.00"},
        {"company": "Minus", "previous": "200.00", "current": "150.00"},
        {"company": "Nova", "previous": None, "current": "80.00"},
    )
    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    rows = response.context["review_rows"]
    for value in ("100,00", "125,00", "+25,00", "+25,00 %", "-50,00", "-25,00 %"):
        assert value in content
    assert rows[2]["previous_display"] == "Nova"
    assert rows[2]["delta_percent_display"] == "—"


@pytest.mark.django_db
def test_step4_nicelna_osnova_prikaze_crtico_in_eur_delta(client):
    _review_cycle({"previous": "0.00", "current": "50.00"})
    row = client.get("/mesecni-obracun/").context["review_rows"][0]
    assert row["previous_display"] == "0,00 €"
    assert row["delta_eur_display"] == "+50,00 €"
    assert row["delta_percent_display"] == "—"


@pytest.mark.django_db
def test_step4_izbira_razlog_in_ponovna_izbira_so_trajni(client):
    _cycle, (review,) = _review_cycle({})
    identity = {"lease_id": review.lease_id, "candidate_fingerprint": review.candidate_fingerprint}
    response = client.post("/mesecni-obracun/", {"action": "deselect_candidate", "non_issue_reason": "  Po dogovoru  ", **identity})
    assert response.status_code == 302
    review.refresh_from_db()
    assert (review.selected, review.non_issue_reason) == (False, "Po dogovoru")
    assert "Po dogovoru" in client.get("/mesecni-obracun/").content.decode()
    response = client.post("/mesecni-obracun/", {"action": "select_candidate", **identity})
    assert response.status_code == 302
    review.refresh_from_db()
    assert (review.selected, review.non_issue_reason) == (True, "")


@pytest.mark.django_db
def test_step4_napake_so_vrsticne_in_ne_spremenijo_stanja(client):
    _cycle, (review,) = _review_cycle({})
    response = client.post("/mesecni-obracun/", {
        "action": "deselect_candidate", "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint, "non_issue_reason": "   ",
    })
    assert response.status_code == 200
    assert "Za neizdajo kandidata je potreben razlog." in response.content.decode()
    review.refresh_from_db()
    assert review.selected is True
    response = client.post("/mesecni-obracun/", {
        "action": "select_candidate", "lease_id": review.lease_id,
        "candidate_fingerprint": "f" * 64,
    })
    assert response.status_code == 200
    assert "Kandidat ni več trenutni kandidat. Osvežite paketni pregled." in response.content.decode()


@pytest.mark.django_db
def test_step4_posamicna_potrditev_izjeme_prezivi_reload(client):
    _cycle, (review,) = _review_cycle({"exceptions": ["Indeksacija."]})
    identity = {"lease_id": review.lease_id, "candidate_fingerprint": review.candidate_fingerprint}
    assert "Potrditev potrebna" in client.get("/mesecni-obracun/").content.decode()
    response = client.post("/mesecni-obracun/", {"action": "confirm_candidate", **identity})
    assert response.status_code == 302
    review.refresh_from_db()
    assert review.confirmed_at is not None
    assert "Potrjeno" in client.get("/mesecni-obracun/").content.decode()
    client.post("/mesecni-obracun/", {"action": "deselect_candidate", "non_issue_reason": "Ne", **identity})
    response = client.post("/mesecni-obracun/", {"action": "confirm_candidate", **identity})
    assert response.status_code == 200
    assert "Neizbranega kandidata ni mogoče potrditi." in response.content.decode()


@pytest.mark.django_db
def test_step4_neizbrana_izjema_ni_pending_in_ponovna_izbira_jo_obnovi(client):
    _cycle, (review,) = _review_cycle({"exceptions": ["Indeksacija."]})
    identity = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }
    client.post(
        "/mesecni-obracun/",
        {
            "action": "deselect_candidate",
            "non_issue_reason": "Ne izdaj ta mesec",
            **identity,
        },
    )

    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    assert response.context["review_summary"]["requiring_confirmation"] == 0
    assert "review-row-attention" not in content
    assert 'class="attention-state">Potrditev potrebna' not in content
    assert "Ročno neizdano" in content
    assert "Ne izdaj ta mesec" in content
    assert "Ponovno izberi" in content

    client.post("/mesecni-obracun/", {"action": "select_candidate", **identity})
    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    assert response.context["review_summary"]["requiring_confirmation"] == 1
    assert "review-row-attention" in content
    assert 'class="attention-state">Potrditev potrebna' in content


@pytest.mark.django_db
def test_step4_sistemsko_neizdajnega_ni_mogoce_izbrati(client):
    _cycle, (review,) = _review_cycle({"will_issue": False, "system_reason": "Ni obračunljivih postavk."})
    response = client.post("/mesecni-obracun/", {
        "action": "select_candidate", "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    })
    assert response.status_code == 200
    content = response.content.decode()
    assert "Ni obračunljivih postavk." in content
    assert "Sistemsko neizdajno" in content
    review.refresh_from_db()
    assert review.selected is False


@pytest.mark.django_db
def test_step4_mutacije_izven_koraka4_ne_ustvarjajo_dokumentov(client):
    cycle, (review,) = _review_cycle({})
    identity = {"lease_id": review.lease_id, "candidate_fingerprint": review.candidate_fingerprint}
    initial = (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count())
    for step in (MonthlyCycle.Korak.IZRACUN_OSNUTKOV, MonthlyCycle.Korak.POTRDITEV_PAKETA):
        cycle.trenutni_korak = step
        cycle.save(update_fields=["trenutni_korak"])
        assert client.post("/mesecni-obracun/", {"action": "deselect_candidate", "non_issue_reason": "Ne", **identity}).status_code == 200
    review.refresh_from_db()
    assert review.selected is True
    assert (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count()) == initial


@pytest.mark.django_db
def test_step4_ne_zaupa_ponarejenim_poslovnim_podatkom(client):
    _cycle, (review,) = _review_cycle({"company": "Pravo podjetje", "current": "125.00"})
    response = client.post("/mesecni-obracun/", {
        "action": "deselect_candidate", "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint, "non_issue_reason": "Ne",
        "company": "Ponarejeno podjetje", "amount": "999999.99", "status": "false",
    }, follow=True)
    content = response.content.decode()
    assert "Pravo podjetje" in content
    assert "Ponarejeno podjetje" not in content
    assert "125,00" in content
    assert "999999" not in content


@pytest.mark.django_db
def test_step4_pdf_preview_uses_exact_current_identity_without_side_effects(
    client, monkeypatch, tmp_path
):
    _cycle, (first, second) = _review_cycle({}, {})
    NumberSequence.objects.create(
        company=first.batch.company,
        tip=NumberSequence.Tip.NP,
        leto=0,
        zadnja_stevilka=7,
    )
    before_reviews = list(MonthlyCandidateReview.objects.values())
    before_snapshots = {
        batch.pk: deepcopy(batch.calculation_snapshot)
        for batch in MonthlyBatch.objects.all()
    }
    before_counts = (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        NumberSequence.objects.count(),
    )
    before_sequences = list(
        NumberSequence.objects.values_list("pk", "zadnja_stevilka")
    )
    before_artifacts = list(
        Invoice.objects.values_list("pk", "rendered_pdf_path", "checksum")
    )
    monkeypatch.setattr(
        "ui.views.generiraj_mesecni_predogled_pdf", lambda snapshot, candidate: b"%PDF-test"
    )

    response = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": first.lease_id,
        "candidate_fingerprint": first.candidate_fingerprint,
    })

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("inline;")
    assert response.content == b"%PDF-test"
    assert list(MonthlyCandidateReview.objects.values()) == before_reviews
    assert {
        batch.pk: batch.calculation_snapshot for batch in MonthlyBatch.objects.all()
    } == before_snapshots
    assert (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        NumberSequence.objects.count(),
    ) == before_counts
    assert list(
        NumberSequence.objects.values_list("pk", "zadnja_stevilka")
    ) == before_sequences
    assert list(
        Invoice.objects.values_list("pk", "rendered_pdf_path", "checksum")
    ) == before_artifacts
    assert list(tmp_path.iterdir()) == []

    wrong_pair = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": first.lease_id,
        "candidate_fingerprint": second.candidate_fingerprint,
    })
    stale = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": first.lease_id,
        "candidate_fingerprint": "f" * 64,
    })
    assert wrong_pair.status_code == 404
    assert stale.status_code == 404
    assert "ni več na voljo" in stale.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "step", [MonthlyCycle.Korak.IZRACUN_OSNUTKOV, MonthlyCycle.Korak.POTRDITEV_PAKETA]
)
def test_pdf_preview_is_rejected_outside_step4(client, step):
    cycle, (review,) = _review_cycle({})
    cycle.trenutni_korak = step
    cycle.save(update_fields=["trenutni_korak"])

    response = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    })

    assert response.status_code == 409
    assert "samo v koraku 4" in response.content.decode()


@pytest.mark.django_db
def test_pdf_preview_renderer_error_is_not_misclassified_as_bad_lease_id(
    client, monkeypatch
):
    _cycle, (review,) = _review_cycle({})
    def fail(_snapshot, _candidate):
        raise PdfRenderNapaka("PDF_GENERIRANJE_NI_USPELO", "Renderer odpovedal.")
    monkeypatch.setattr("ui.views.generiraj_mesecni_predogled_pdf", fail)

    response = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    })

    assert response.status_code == 500
    assert response.content.decode() == "Predogleda PDF ni bilo mogoče ustvariti."


@pytest.mark.django_db
def test_pdf_preview_unsupported_template_returns_controlled_error(
    client, monkeypatch
):
    _cycle, (review,) = _review_cycle({})

    def fail(_snapshot, _candidate):
        raise PdfRenderNapaka(
            "PREDLOGA_NEPODPRTA",
            "Shranjene različice PDF predloge ni mogoče prikazati.",
        )

    monkeypatch.setattr("ui.views.generiraj_mesecni_predogled_pdf", fail)
    response = client.get("/mesecni-obracun/predogled-pdf/", {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    })

    assert response.status_code == 422
    assert "Shranjene različice PDF predloge" in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"lease_id": "abc", "candidate_fingerprint": "a" * 64}, "ID pogodbe ni veljaven."),
        ({"lease_id": "1"}, "Manjka identiteta kandidata."),
        ({"candidate_fingerprint": "a" * 64}, "ID pogodbe ni veljaven."),
    ],
)
def test_pdf_preview_rejects_malformed_or_missing_identity(client, params, message):
    response = client.get("/mesecni-obracun/predogled-pdf/", params)

    assert response.status_code < 500
    assert message in response.content.decode()


@pytest.mark.django_db
def test_pdf_preview_rejects_real_stale_fingerprint_after_snapshot_replacement(
    client, monkeypatch
):
    _cycle, (review,) = _review_cycle({})
    monkeypatch.setattr(
        "ui.views.generiraj_mesecni_predogled_pdf",
        lambda snapshot, candidate: b"%PDF-test",
    )
    before = (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values_list("pk", "zadnja_stevilka")),
    )
    url = "/mesecni-obracun/predogled-pdf/"
    identity_a = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }

    assert client.get(url, identity_a).status_code == 200

    batch = review.batch
    snapshot = batch.calculation_snapshot
    fingerprint_b = "b" * 64
    snapshot["candidates"][0]["candidate_fingerprint"] = fingerprint_b
    batch.calculation_snapshot = snapshot
    batch.save(update_fields=["calculation_snapshot"])

    assert client.get(url, identity_a).status_code == 404
    assert client.get(url, {
        "lease_id": review.lease_id,
        "candidate_fingerprint": fingerprint_b,
    }).status_code == 200
    assert (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values_list("pk", "zadnja_stevilka")),
    ) == before


@pytest.mark.django_db
def test_step4_issuable_candidate_shows_preview_without_visible_fingerprint(client):
    _cycle, (review,) = _review_cycle({})

    content = client.get("/mesecni-obracun/").content.decode()
    visible_text = " ".join(re.findall(r">([^<]+)<", content))

    assert ">Predogled PDF</a>" in content
    assert review.candidate_fingerprint not in visible_text


@pytest.mark.django_db
def test_zamrznitev_veljavnega_paketa_ne_ustvari_dokumentov():
    cycle, (review,) = _review_cycle({})
    review.requires_confirmation = False
    review.save(update_fields=["requires_confirmation"])
    before = (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values()),
        deepcopy(review.batch.calculation_snapshot),
        list(MonthlyCandidateReview.objects.values()),
    )

    with pytest.raises(
        NeveljavenPrehodMesecnegaCikla, match="potrditev in zamrznitev paketa"
    ):
        premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.POTRDITEV_PAKETA)
    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED

    frozen = zamrzni_paket_za_potrditev(cycle)

    assert frozen.trenutni_korak == MonthlyCycle.Korak.POTRDITEV_PAKETA
    review.batch.refresh_from_db()
    assert (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values()),
        review.batch.calculation_snapshot,
        list(MonthlyCandidateReview.objects.values()),
    ) == before


@pytest.mark.django_db
def test_zamrznitev_zavrne_paket_brez_aktivnega_izracuna_in_dokumentov():
    cycle, (review,) = _review_cycle({})
    review.batch.preflight_status = MonthlyBatch.PreflightStatus.BLOCKED
    review.batch.calculation_status = MonthlyBatch.CalculationStatus.BLOCKED
    review.batch.save(update_fields=["preflight_status", "calculation_status"])
    before = (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values()),
    )

    with pytest.raises(ValidationError, match="nima veljavnega aktivnega izračuna"):
        zamrzni_paket_za_potrditev(cycle)

    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED
    assert (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        list(NumberSequence.objects.values()),
    ) == before


@pytest.mark.django_db
def test_blokirano_podjetje_ne_blokira_veljavnega_aktivnega_paketa():
    cycle, reviews = _review_cycle({}, {})
    blocked = reviews[1].batch
    blocked.preflight_status = MonthlyBatch.PreflightStatus.BLOCKED
    blocked.calculation_status = MonthlyBatch.CalculationStatus.BLOCKED
    blocked.save(update_fields=["preflight_status", "calculation_status"])

    frozen = zamrzni_paket_za_potrditev(cycle)

    assert frozen.trenutni_korak == MonthlyCycle.Korak.POTRDITEV_PAKETA


@pytest.mark.django_db
def test_zamrznitev_zavrne_napacen_korak_in_manjkajoc_ali_stari_pregled():
    cycle, (review,) = _review_cycle({})
    cycle.trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    cycle.save(update_fields=["trenutni_korak"])
    with pytest.raises(MesecniCikelNapaka, match="samo v koraku 4"):
        zamrzni_paket_za_potrditev(cycle)
    cycle.trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    cycle.save(update_fields=["trenutni_korak"])
    review.delete()
    with pytest.raises(ValidationError, match="Manjka trenutni pregled"):
        zamrzni_paket_za_potrditev(cycle)
    MonthlyCandidateReview.objects.create(
        batch=review.batch,
        lease=review.lease,
        candidate_fingerprint="f" * 64,
        selected=True,
        requires_confirmation=False,
    )
    with pytest.raises(ValidationError, match="Prstni odtis"):
        zamrzni_paket_za_potrditev(cycle)


@pytest.mark.django_db
def test_zamrznitev_zahteva_samo_potrditev_oznacene_izjeme():
    cycle, (review,) = _review_cycle({"exceptions": ["Indeksacija."]})
    assert review.reviewed_at is None
    with pytest.raises(ValidationError, match="ni potrjena"):
        zamrzni_paket_za_potrditev(cycle)
    review.confirmed_at = timezone.now()
    review.save(update_fields=["confirmed_at"])
    assert zamrzni_paket_za_potrditev(cycle).trenutni_korak == 5


@pytest.mark.django_db
def test_zamrznitev_normalnega_izbranega_ne_zahteva_reviewed_at():
    cycle, (review,) = _review_cycle({})
    assert review.reviewed_at is None
    assert zamrzni_paket_za_potrditev(cycle).trenutni_korak == 5


@pytest.mark.django_db
def test_zamrznitev_neizbranega_zahteva_razlog_in_dovoli_nic_izbranih():
    cycle, (review,) = _review_cycle({})
    review.selected = False
    review.save(update_fields=["selected"])
    with pytest.raises(ValidationError, match="Manjka razlog neizdaje"):
        zamrzni_paket_za_potrditev(cycle)
    review.non_issue_reason = "Po odločitvi uporabnika"
    review.save(update_fields=["non_issue_reason"])
    assert zamrzni_paket_za_potrditev(cycle).trenutni_korak == 5


@pytest.mark.django_db
def test_sistemsko_neizdajni_ne_zahteva_potrditve_a_zahteva_razlog():
    cycle, (review,) = _review_cycle({"will_issue": False})
    review.requires_confirmation = True
    review.save(update_fields=["requires_confirmation"])
    with pytest.raises(ValidationError, match="nima razloga"):
        zamrzni_paket_za_potrditev(cycle)
    snapshot = review.batch.calculation_snapshot
    snapshot["candidates"][0]["non_issue_reason"] = "Ni obračunljivih postavk."
    review.batch.calculation_snapshot = snapshot
    review.batch.save(update_fields=["calculation_snapshot"])
    assert zamrzni_paket_za_potrditev(cycle).trenutni_korak == 5


@pytest.mark.django_db
def test_zamrznitev_zavrne_nepopoln_ali_neskladen_izdajni_snapshot():
    cycle, (review,) = _review_cycle({})
    snapshot = review.batch.calculation_snapshot
    snapshot["candidates"][0]["lines"] = []
    review.batch.calculation_snapshot = snapshot
    review.batch.save(update_fields=["calculation_snapshot"])
    with pytest.raises(ValidationError, match="nepopoln"):
        zamrzni_paket_za_potrditev(cycle)
    snapshot["candidates"][0]["lines"] = [
        {"net": "1.00", "vat": "0.00", "total": "1.00"}
    ]
    review.batch.calculation_snapshot = snapshot
    review.batch.save(update_fields=["calculation_snapshot"])
    with pytest.raises(ValidationError, match="niso skladne"):
        zamrzni_paket_za_potrditev(cycle)


@pytest.mark.django_db
def test_step4_action_in_step5_reload_prikazeta_zamrznjen_snapshot(client):
    _cycle, reviews = _review_cycle(
        {"company": "Snapshot podjetje", "tenant": "Snapshot najemnik"},
        {"will_issue": False, "system_reason": "Sistemski razlog"},
    )
    first, _second = reviews
    first.selected = False
    first.non_issue_reason = "Uporabnikov razlog"
    first.save(update_fields=["selected", "non_issue_reason"])
    before_counts = (
        Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count()
    )

    response = client.post("/mesecni-obracun/", {"action": "freeze_package"})
    assert response.status_code == 302
    first.batch.company.naziv = "Spremenjeno podjetje"
    first.batch.company.save(update_fields=["naziv"])
    first.lease.tenant.naziv = "Spremenjen najemnik"
    first.lease.tenant.save(update_fields=["naziv"])
    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "Potrditev paketa" in content
    assert "Paket je zaklenjen" in content
    assert "Snapshot podjetje" in content and "Snapshot najemnik" in content
    assert "Spremenjeno podjetje" not in content and "Spremenjen najemnik" not in content
    assert "Uporabnikov razlog" in content and "Sistemski razlog" in content
    assert 'class="wizard-step is-active" aria-current="step"><span>5</span>' in content
    assert '<div class="wizard-step"><span>6</span>' in content
    assert (
        Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count()
    ) == before_counts


@pytest.mark.django_db
def test_step4_gumb_in_nepotrjena_izjema_vidno_blokira(client):
    _review_cycle({"exceptions": ["Indeksacija."]})
    response = client.get("/mesecni-obracun/")
    content = response.content.decode()
    assert "Nadaljuj na potrditev" in content
    assert "Potrditev potrebna <strong>1</strong>" in content
    assert 'value="freeze_package" type="submit" disabled' in content
    response = client.post("/mesecni-obracun/", {"action": "freeze_package"})
    assert response.status_code == 200
    assert "Izjema izbranega kandidata ni potrjena." in response.content.decode()


@pytest.mark.django_db
def test_step5_zavrne_vse_mutacije_in_ponovljen_post(client):
    cycle, (review,) = _review_cycle({})
    zamrzni_paket_za_potrditev(cycle)
    identity = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }
    before = (deepcopy(review.batch.calculation_snapshot), list(MonthlyCandidateReview.objects.values()))
    for action in (
        "deselect_candidate", "confirm_candidate", "rerun_preflight",
        "rerun_calculation", "refresh_review", "freeze_package",
    ):
        response = client.post("/mesecni-obracun/", {"action": action, **identity})
        assert response.status_code < 500
    review.batch.refresh_from_db()
    assert (review.batch.calculation_snapshot, list(MonthlyCandidateReview.objects.values())) == before
    assert MonthlyCycle.objects.get(pk=cycle.pk).trenutni_korak == 5


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action",
    [
        "freeze_package",
        "issue_frozen_package",
        "render_final_pdfs",
        "prepare_delivery",
        "send_delivery",
    ],
)
def test_zamrznitev_in_izdaja_post_sta_csrf_zascitena(action):
    _review_cycle({})
    response = Client(enforce_csrf_checks=True).post(
        "/mesecni-obracun/", {"action": action}
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_korak_6_je_aktiven_in_ne_zakljucen(client):
    MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    content = client.get("/mesecni-obracun/").content.decode()
    assert "Zaključek ciklusa" in content
    assert "Pošiljanje, preverjanje in arhiviranje še niso izvedeni" in content
    assert 'is-completed"><span>✓</span><strong>Potrditev paketa' in content
    assert 'is-active" aria-current="step"><span>6</span><strong>Zaključek ciklusa' in content


@pytest.mark.django_db
def test_prepare_delivery_izven_koraka_6_ne_klice_storitve(client, monkeypatch):
    _review_cycle({})
    monkeypatch.setattr(
        "ui.views.pripravi_dostavo_mesecnega_cikla",
        lambda *_args: pytest.fail("Storitev se izven koraka 6 ne sme klicati."),
    )

    response = client.post("/mesecni-obracun/", {"action": "prepare_delivery"})

    assert response.status_code == 200
    assert "Priprava dostave je dovoljena samo v koraku 6." in response.content.decode()
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.django_db
def test_step6_get_prikaze_avtoritativno_nepripravljeno_dostavo_in_pdf(
    client, ready_company, settings, tmp_path  # noqa: F811
):
    _step6_cycle(ready_company, settings, tmp_path)
    invoice = Invoice.objects.get()

    response = client.get("/mesecni-obracun/")
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["delivery_state"]["selected_count"] == 1
    assert response.context["delivery_state"]["pending_count"] == 1
    assert invoice.company.naziv in content
    assert invoice.stevilka in content
    assert "Ni pripravljeno" in content
    assert "Dostava še ni pripravljena." in content
    assert "PDF pripravljen" in content
    assert "Pripravi pošiljanje" in content
    assert "Pošlji pripravljene račune" not in content


@pytest.mark.django_db
def test_step6_prepare_delivery_je_idempotenten_brez_zunanjih_operacij(
    client, ready_company, settings, tmp_path, monkeypatch  # noqa: F811
):
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    invoice = Invoice.objects.get()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Zunanja operacija se med pripravo dostave ne sme izvesti.")

    monkeypatch.setattr("delivery.services.send_pdf_email", forbidden)
    monkeypatch.setattr("keyring.get_password", forbidden)
    monkeypatch.setattr("archive.services.archive_pdf", forbidden)

    first = client.post("/mesecni-obracun/", {"action": "prepare_delivery"})
    assert first.status_code == 302
    attempt = DeliveryAttempt.objects.get(invoice=invoice)
    assert (attempt.sequence, attempt.result) == (1, DeliveryAttempt.Result.PREPARED)

    content = client.get("/mesecni-obracun/").content.decode()
    assert "Pripravljeno za pošiljanje" in content
    assert "Prvi poskus dostave je pripravljen." in content
    assert "Pripravljeni računi še niso poslani." in content
    assert "Pošlji pripravljene račune" in content
    assert "Pripravi pošiljanje" not in content
    assert "PDF pripravljen" in content

    second = client.post("/mesecni-obracun/", {"action": "prepare_delivery"})
    assert second.status_code == 302
    assert DeliveryAttempt.objects.filter(invoice=invoice).count() == 1
    cycle.refresh_from_db()
    assert (cycle.trenutni_korak, cycle.status) == (
        MonthlyCycle.Korak.ZAKLJUCEK,
        MonthlyCycle.Status.V_TEKU,
    )


@pytest.mark.django_db
def test_sporocilo_uporabi_nespremenljivi_posnetek_izdajatelja():
    company = ustvari_podjetje("Poznejše ime d.o.o.", "24561004")
    invoice = Invoice(
        company=company,
        stevilka="NP-00001",
        izdajatelj_naziv="Alfa d.o.o.",
    )

    subject, body = _sporocilo_mesecnega_racuna(invoice)

    assert subject == "Račun NP-00001 – Alfa d.o.o."
    assert body == (
        "Spoštovani,\n\n"
        "v priponki vam pošiljamo račun NP-00001.\n\n"
        "Lep pozdrav,\n"
        "Alfa d.o.o."
    )
    assert company.naziv not in subject + body


@pytest.mark.parametrize(
    ("number", "issuer"),
    [("NP-00001", "Alfa d.o.o."), ("ST-2026-00002", "Beta d.o.o.")],
)
def test_sporocilo_ima_enako_predlogo_za_vsa_podjetja(number, issuer):
    subject, body = _sporocilo_mesecnega_racuna(
        Invoice(stevilka=number, izdajatelj_naziv=issuer)
    )

    assert subject == f"Račun {number} – {issuer}"
    assert body == (
        f"Spoštovani,\n\nv priponki vam pošiljamo račun {number}.\n\n"
        f"Lep pozdrav,\n{issuer}"
    )


def _smtp_settings(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "marjan-smtp"


@pytest.mark.django_db
def test_step6_post_poslje_pripravljeno_enkrat_in_ohrani_posnetke(
    client, ready_company, settings, tmp_path, monkeypatch  # noqa: F811
):
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    invoice = Invoice.objects.get()
    invoice.izdajatelj_naziv = "Alfa d.o.o."
    invoice.save(update_fields=["izdajatelj_naziv"])
    pripravi_dostavo_mesecnega_cikla(cycle)
    attempt = DeliveryAttempt.objects.get()
    frozen = (
        attempt.recipients_to,
        attempt.recipients_cc,
        attempt.recipients_bcc,
        attempt.pdf_path,
        attempt.pdf_checksum,
    )
    _smtp_settings(settings)
    calls = []

    def send(prepared_attempt, **kwargs):
        calls.append((prepared_attempt.pk, kwargs))
        return mark_accepted(
            mark_submission_started(prepared_attempt), code="SMTP_ACCEPTED"
        )

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    monkeypatch.setattr(
        "archive.services.archive_pdf",
        lambda *_args, **_kwargs: pytest.fail("Arhiviranje se ne sme izvesti."),
    )

    first = client.post("/mesecni-obracun/", {"action": "send_delivery"})
    second = client.post("/mesecni-obracun/", {"action": "send_delivery"})

    assert first.status_code == second.status_code == 302
    assert len(calls) == 1
    assert calls[0][1]["subject"] == "Račun NP-00001 – Alfa d.o.o."
    assert calls[0][1]["body"].endswith("Lep pozdrav,\nAlfa d.o.o.")
    assert calls[0][1]["smtp_config"].username == invoice.company.email_posiljatelj
    assert calls[0][1]["credential_ref"].username == invoice.company.keychain_kljuc_posiljatelja
    invoice.refresh_from_db()
    attempt.refresh_from_db()
    cycle.refresh_from_db()
    assert invoice.status == Invoice.Status.POSLAN
    assert attempt.result == DeliveryAttempt.Result.ACCEPTED
    assert (
        attempt.recipients_to,
        attempt.recipients_cc,
        attempt.recipients_bcc,
        attempt.pdf_path,
        attempt.pdf_checksum,
    ) == frozen
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )
    content = client.get("/mesecni-obracun/").content.decode()
    assert "Poslano" in content
    assert "Pošiljanje je končano. Arhiviranje in končni zaključek še nista izvedena." in content
    assert "Pripravi pošiljanje" not in content
    assert "Pošlji pripravljene račune" not in content


@pytest.mark.django_db
@pytest.mark.parametrize("terminal", ["failed", "unknown"])
def test_step6_post_ne_ponovi_neuspesnega_ali_neznanega(
    client, ready_company, settings, tmp_path, monkeypatch, terminal  # noqa: F811
):
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(cycle)
    attempt = mark_submission_started(DeliveryAttempt.objects.get())
    if terminal == "failed":
        mark_confirmed_failure(attempt, code="SMTP_REJECTED")
    else:
        mark_result_unknown(attempt, code="SMTP_RESULT_UNKNOWN")
    monkeypatch.setattr(
        "delivery.services.send_pdf_email",
        lambda *_args, **_kwargs: pytest.fail("Končnega stanja se ne sme poslati znova."),
    )

    response = client.post("/mesecni-obracun/", {"action": "send_delivery"})

    assert response.status_code == 302
    assert DeliveryAttempt.objects.count() == 1


@pytest.mark.django_db
def test_step6_post_ne_ponovi_dostave_v_teku(
    client, ready_company, settings, tmp_path, monkeypatch  # noqa: F811
):
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(cycle)
    attempt = mark_submission_started(DeliveryAttempt.objects.get())
    monkeypatch.setattr(
        "delivery.services.send_pdf_email",
        lambda *_args, **_kwargs: pytest.fail("Dostave v teku se ne sme poslati znova."),
    )

    response = client.post("/mesecni-obracun/", {"action": "send_delivery"})

    assert response.status_code == 302
    assert DeliveryAttempt.objects.count() == 1
    attempt.refresh_from_db()
    assert attempt.result == DeliveryAttempt.Result.SUBMISSION_STARTED


@pytest.mark.django_db
def test_send_delivery_izven_koraka_6_ne_klice_storitve(client, monkeypatch):
    _review_cycle({})
    monkeypatch.setattr(
        "ui.views.poslji_dostavo_mesecnega_cikla",
        lambda *_args: pytest.fail("Pošiljanje izven koraka 6 ni dovoljeno."),
    )

    response = client.post("/mesecni-obracun/", {"action": "send_delivery"})

    assert response.status_code == 200
    assert "Pošiljanje je dovoljeno samo v koraku 6." in response.content.decode()


@pytest.mark.django_db
def test_get_ne_posilja(client, ready_company, settings, tmp_path, monkeypatch):  # noqa: F811
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(cycle)
    monkeypatch.setattr(
        "ui.views.poslji_dostavo_mesecnega_cikla",
        lambda *_args: pytest.fail("GET ne sme pošiljati."),
    )

    assert client.get("/mesecni-obracun/").status_code == 200


@pytest.mark.django_db
def test_napaka_nastavitve_enega_podjetja_ne_blokira_drugega(
    client, monkeypatch
):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    bad_company = ustvari_podjetje("Napačno d.o.o.", "15012557")
    good_company = ustvari_podjetje("Veljavno d.o.o.", "69165028")
    invoices = [
        Invoice.objects.create(
            company=company,
            document_type=Invoice.DocumentType.NP,
            stevilka=f"NP-0000{index}",
            izdajatelj_naziv=company.naziv,
            datum_izdaje=date(2026, 9, 30),
            obdobje_od=date(2026, 9, 1),
            obdobje_do=date(2026, 9, 30),
        )
        for index, company in enumerate((bad_company, good_company), start=1)
    ]
    state = {
        "selected_count": 2,
        "sent_count": 0,
        "failed_count": 0,
        "unknown_count": 0,
        "pending_count": 2,
        "conflict_count": 0,
        "complete": False,
        "per_invoice": [
            {
                "invoice_id": invoice.pk,
                "invoice_number": invoice.stevilka,
                "issuer_label": invoice.izdajatelj_naziv,
                "delivery_state": "DELIVERY_PREPARED",
                "message": "Pripravljeno.",
            }
            for invoice in invoices
        ],
    }
    monkeypatch.setattr(
        "ui.views.stanje_dostave_mesecnega_cikla", lambda _cycle: state
    )

    def smtp_config(company):
        if company.pk == bad_company.pk:
            from delivery.configuration import SmtpConfigurationError

            raise SmtpConfigurationError("password=NE_RAZKRIJ")
        return SmtpConfig(
            host="smtp.example.com",
            port=587,
            security=SmtpSecurity.STARTTLS,
            timeout_seconds=10,
            username=company.email_posiljatelj,
        )

    monkeypatch.setattr("ui.views.resolve_smtp_config", smtp_config)
    monkeypatch.setattr(
        "ui.views.resolve_keyring_credential_ref",
        lambda company: KeyringCredentialRef(
            "marjan-smtp", company.keychain_kljuc_posiljatelja
        ),
    )
    calls = []
    monkeypatch.setattr(
        "ui.views.poslji_dostavo_mesecnega_cikla",
        lambda called_cycle, inputs: calls.append((called_cycle.pk, inputs)) or state,
    )
    monkeypatch.setattr(
        "delivery.services.send_pdf_email",
        lambda *_args, **_kwargs: pytest.fail("UI ne sme neposredno klicati SMTP."),
    )

    response = client.post("/mesecni-obracun/", {"action": "send_delivery"})

    assert response.status_code == 200
    assert len(calls) == 1 and calls[0][0] == cycle.pk
    assert len(calls[0][1]) == 1
    sent_input = calls[0][1][0]
    assert sent_input.invoice_id == invoices[1].pk
    assert sent_input.smtp_config.username == good_company.email_posiljatelj
    assert sent_input.credential_ref.username == good_company.keychain_kljuc_posiljatelja
    assert sent_input.subject == "Račun NP-00002 – Veljavno d.o.o."
    content = response.content.decode()
    assert "Za račun NP-00001 SMTP nastavitev ni mogoče uporabiti." in content
    assert "NE_RAZKRIJ" not in content
