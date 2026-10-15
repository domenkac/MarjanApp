import re
from contextlib import nullcontext
from copy import deepcopy
from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from archive.configuration import ArchiveConfigurationError
from archive.models import ArchiveConfiguration
from companies.models import Company, NumberSequence
from contracts.models import Lease, Property, RecipientContact, Tenant, Unit
from core.models import AuditLogEntry
from delivery.credentials import KeyringCredentialRef
from delivery.execution import delivery_execution_lock
from delivery.models import DeliveryAttempt
from delivery.services import (
    UNKNOWN_RESOLUTION_FIELD,
    UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED,
    UNKNOWN_RESOLUTION_SENT_VERIFIED,
    UNKNOWN_RESOLUTION_UNRESOLVED,
    mark_accepted,
    mark_confirmed_failure,
    mark_result_unknown,
    mark_submission_started,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
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
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture
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
            "tenant": {
                "name": tenant.naziv, "type": tenant.tip,
                "vat_registered": tenant.identificiran_za_ddv,
                "full_deduction": tenant.ddv_zavezanec_s_polnim_odbitkom,
            },
            "lines": [
                {
                    "vat_regime_code": "TEST_STANDARD",
                    "requires_full_deduction": False,
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


def _final_control_state(*, successful=True, category=None):
    issue = []
    if category:
        issue = [{
            "issuer_label": "Alfa d.o.o.",
            "invoice_number": "NP-00001",
            "category": category,
            "message": "Konkretna težava.",
            "delivery_result_code": "SMTP_REJECTED" if category == "DELIVERY_FAILED" else "",
            "delivery_result_message": "Strežnik je zavrnil sporočilo." if category == "DELIVERY_FAILED" else "",
        }]
    return {
        "delivery_complete": successful,
        "archive_complete": successful,
        "all_required_work_successful": successful,
        "requires_attention": not successful,
        "headline": "Mesečni postopek uspešno zaključen" if successful else "Mesečni postopek zahteva pozornost",
        "prepared_count": 0 if successful else 3,
        "sent_count": 0 if successful else 2,
        "intentionally_unissued_count": 2,
        "failed_count": 0 if successful else 1,
        "unknown_count": 0,
        "delivery_pending_count": 0,
        "archive_verified_count": 0 if successful else 2,
        "archive_pending_count": 0,
        "conflict_count": 1 if category == "PACKAGE_CONFLICT" else 0,
        "per_company": [{
            "issuer_label": "Alfa d.o.o.",
            "prepared_count": 0 if successful else 3,
            "sent_count": 0 if successful else 2,
            "intentionally_unissued_count": 2,
            "archive_verified_count": 0 if successful else 2,
            "failed_count": 0 if successful else 1,
            "unknown_count": 0,
            "delivery_pending_count": 0,
            "archive_issue_count": 0 if successful else 1,
            "requires_attention": not successful,
        }],
        "issues": issue,
    }


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


@pytest.mark.django_db(transaction=True)
def test_veljavna_izbira_uporabi_prg_in_ne_ustvari_racuna(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2026", "mesec": "8", "datum_izdaje": "2026-08-12"},
    )

    assert response.status_code == 302
    assert response.url == "/mesecni-obracun/?backup_error=1"
    cycle = MonthlyCycle.objects.get()
    assert cycle.trenutni_korak == 2
    assert MonthlyBatch.objects.count() == 1
    assert Invoice.objects.count() == 0


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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
    save_financial_fixture(first.batch.company, update_fields=["naziv"])
    first.lease.tenant.naziv = "Spremenjen najemnik"
    save_business_record(first.lease.tenant, update_fields=["naziv"])
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
        "archive_sent_invoices",
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
def test_step6_get_nalozi_uspesno_zakljucno_kontrolo_brez_mutacij(
    client, monkeypatch
):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    state = _final_control_state()
    calls = []
    monkeypatch.setattr("ui.views.resolve_archive_roots", lambda: object())
    monkeypatch.setattr("ui.views.stanje_arhiviranja_mesecnega_cikla", lambda _cycle: {"per_invoice": []})
    monkeypatch.setattr(
        "ui.views.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda received: calls.append(received.pk) or state,
    )
    forbidden = lambda *_args, **_kwargs: pytest.fail("GET ne sme izvajati operacij.")
    monkeypatch.setattr("ui.views.poslji_dostavo_mesecnega_cikla", forbidden)
    monkeypatch.setattr("ui.views.arhiviraj_poslane_racune_mesecnega_cikla", forbidden)
    monkeypatch.setattr("ui.views.odpri_zakljucek_mesecnega_cikla", forbidden)

    content = client.get("/mesecni-obracun/").content.decode()

    cycle.refresh_from_db()
    assert calls == [cycle.pk]
    assert "Mesečni postopek uspešno zaključen" in content
    assert "Zaključna kontrola je uspešna. Mesečni cikel še ni zaprt." in content
    assert "Namenoma neizdani <strong>2</strong>" in content
    assert "Alfa d.o.o." in content
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.django_db
def test_step6_brez_nastavitev_ne_klice_zakljucne_kontrole(client, monkeypatch):
    MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    monkeypatch.setattr(
        "ui.views.resolve_archive_roots",
        lambda: (_ for _ in ()).throw(ArchiveConfigurationError("Manjka.")),
    )
    monkeypatch.setattr(
        "ui.views.stanje_arhiviranja_mesecnega_cikla",
        lambda *_args: pytest.fail("Preverjanje arhiva mora biti preskočeno."),
    )
    monkeypatch.setattr(
        "ui.views.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda *_args: pytest.fail("Zaključna kontrola mora biti preskočena."),
    )

    content = client.get("/mesecni-obracun/").content.decode()

    assert "Lokacije arhiva niso konfigurirane." in content
    assert "Mesečni postopek" not in content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("category", "label"),
    [
        ("DELIVERY_FAILED", "Dostava ni uspela"),
        ("DELIVERY_UNKNOWN", "Rezultat dostave ni znan"),
        ("ARCHIVE_NOT_STARTED", "Arhiviranje ni začeto"),
        ("ARCHIVE_DIVERGENCE", "Neskladje arhiva"),
        ("PACKAGE_CONFLICT", "Konflikt paketa"),
    ],
)
def test_step6_prikaze_težavo_zakljucne_kontrole(
    client, monkeypatch, category, label
):
    MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    monkeypatch.setattr("ui.views.resolve_archive_roots", lambda: object())
    monkeypatch.setattr("ui.views.stanje_arhiviranja_mesecnega_cikla", lambda _cycle: {"per_invoice": []})
    monkeypatch.setattr(
        "ui.views.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda _cycle: _final_control_state(successful=False, category=category),
    )

    content = client.get("/mesecni-obracun/").content.decode()

    assert "Mesečni postopek zahteva pozornost" in content
    assert label in content
    assert "Konkretna težava." in content
    assert "Namenoma neizdani <strong>2</strong>" in content
    assert "Alfa d.o.o." in content


@pytest.mark.django_db
def test_step6_prikaze_stetja_podjetja_iz_rezultata_storitve(client, monkeypatch):
    MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 30),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )
    state = _final_control_state(successful=False)
    state["per_company"][0].update({
        "prepared_count": 8,
        "sent_count": 7,
        "intentionally_unissued_count": 6,
        "archive_verified_count": 5,
        "failed_count": 4,
        "unknown_count": 3,
        "delivery_pending_count": 2,
        "archive_issue_count": 1,
    })
    monkeypatch.setattr("ui.views.resolve_archive_roots", lambda: object())
    monkeypatch.setattr(
        "ui.views.stanje_arhiviranja_mesecnega_cikla",
        lambda _cycle: {"per_invoice": []},
    )
    monkeypatch.setattr(
        "ui.views.stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda _cycle: state,
    )

    content = client.get("/mesecni-obracun/").content.decode()

    assert (
        "<strong>Alfa d.o.o.</strong></td><td>8</td><td>7</td><td>6</td>"
        "<td>5</td><td>4</td><td>3</td><td>2</td><td>1</td>"
        "<td>Zahteva pozornost</td>"
    ) in content


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


def _unknown_ui_snapshot():
    return {
        model: list(model.objects.order_by("pk").values())
        for model in (
            Invoice, InvoiceLine, MonthlyCycle, MonthlyBatch, MonthlyCandidateReview,
            NumberSequence, DeliveryAttempt, AuditLogEntry,
        )
    }


@pytest.fixture
def unknown_delivery(ready_company, settings, tmp_path):  # noqa: F811
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(cycle)
    attempt = mark_result_unknown(
        mark_submission_started(DeliveryAttempt.objects.get()), code="SMTP_RESULT_UNKNOWN"
    )
    unrelated = Invoice.objects.values().get(pk=attempt.invoice_id)
    unrelated.pop("id")
    unrelated["stevilka"] = "NP-99999"
    unrelated["lease_id"] = None
    unrelated["obdobje_od"] = date(2025, 1, 1)
    unrelated["obdobje_do"] = date(2025, 1, 31)
    Invoice.objects.create(**unrelated)
    ArchiveConfiguration.objects.update_or_create(
        pk=1, defaults={"local_root": str(tmp_path / "local"),
                       "icloud_root": str(tmp_path / "icloud"),
                       "verified_at": timezone.now()},
    )
    return cycle, attempt


@pytest.mark.django_db
def test_unknown_resolution_get_is_read_only_and_shows_both_decisions(
    client, unknown_delivery, monkeypatch
):
    _cycle, attempt = unknown_delivery
    before = _unknown_ui_snapshot()
    forbidden = lambda *a, **k: pytest.fail("GET must not resolve delivery")
    monkeypatch.setattr("ui.views.resolve_unknown_as_sent", forbidden)
    monkeypatch.setattr("ui.views.resolve_unknown_user_confirmed_not_sent_and_prepare_retry", forbidden)
    response = client.get("/mesecni-obracun/", {
        "action": "unknown_sent", "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Checked",
    })
    content = response.content.decode()
    assert "Rezultat ni znan – potrebno preverjanje." in content
    assert "Preverite mapo Poslano pri ponudniku e-pošte." in content
    assert "Potrdi, da je bilo poslano" in content
    assert "Potrdi, da ni bilo poslano" in content
    assert f'name="invoice_id" value="{attempt.invoice_id}"' in content
    assert f'name="attempt_id" value="{attempt.pk}"' in content
    assert 'name="reason" required' in content
    assert _unknown_ui_snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["not_prepared", "prepared", "started", "sent", "failed", "conflict"])
def test_other_delivery_states_have_no_unknown_form(
    client, ready_company, settings, tmp_path, state  # noqa: F811
):
    cycle = _step6_cycle(ready_company, settings, tmp_path)
    if state != "not_prepared":
        pripravi_dostavo_mesecnega_cikla(cycle)
        attempt = DeliveryAttempt.objects.get()
        if state in {"started", "sent", "failed"}:
            attempt = mark_submission_started(attempt)
        if state == "sent":
            mark_accepted(attempt, code="SMTP_ACCEPTED")
        elif state == "failed":
            mark_confirmed_failure(attempt, code="SMTP_REJECTED")
        elif state == "conflict":
            Invoice.objects.filter(pk=attempt.invoice_id).update(checksum="f" * 64)
    with delivery_execution_lock(attempt.pk) if state == "started" else nullcontext():
        content = client.get("/mesecni-obracun/").content.decode()
    assert 'value="unknown_sent"' not in content
    assert 'value="unknown_not_sent"' not in content
    assert 'name="reason"' not in content


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_csrf_rejected_without_mutation(unknown_delivery, action):
    _cycle, attempt = unknown_delivery
    before = _unknown_ui_snapshot()
    response = Client(enforce_csrf_checks=True).post("/mesecni-obracun/", {
        "action": action, "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Checked",
    })
    assert response.status_code == 403
    assert _unknown_ui_snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
@pytest.mark.parametrize("invalid", ["empty", "blank", "missing_reason", "wrong_invoice", "wrong_attempt", "malformed", "missing_id", "nonlatest", "outside_step6", "outside_package"])
def test_unknown_resolution_invalid_post_is_prg_without_mutation(
    client, unknown_delivery, action, invalid
):
    cycle, attempt = unknown_delivery
    data = {
        "action": action, "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Checked",
    }
    if invalid == "empty":
        data["reason"] = ""
    elif invalid == "blank":
        data["reason"] = "   "
    elif invalid == "missing_reason":
        data.pop("reason")
    elif invalid == "wrong_invoice":
        data["invoice_id"] = Invoice.objects.exclude(pk=attempt.invoice_id).get().pk
    elif invalid == "wrong_attempt":
        data["attempt_id"] = 999999
    elif invalid == "malformed":
        data["attempt_id"] = "invalid"
    elif invalid == "missing_id":
        data.pop("invoice_id")
    elif invalid == "nonlatest":
        retry = DeliveryAttempt.objects.values().get(pk=attempt.pk)
        retry.pop("id")
        retry["sequence"] += 1
        DeliveryAttempt.objects.create(**retry)
    elif invalid == "outside_step6":
        MonthlyCycle.objects.filter(pk=cycle.pk).update(trenutni_korak=5)
    elif invalid == "outside_package":
        outsider = Invoice.objects.exclude(pk=attempt.invoice_id).get()
        retry = DeliveryAttempt.objects.values().get(pk=attempt.pk)
        retry.pop("id")
        retry["invoice_id"] = outsider.pk
        outside_attempt = DeliveryAttempt.objects.create(**retry)
        data.update(invoice_id=outsider.pk, attempt_id=outside_attempt.pk)
    before = _unknown_ui_snapshot()
    response = client.post("/mesecni-obracun/", data)
    assert response.status_code == 302
    assert response["Location"] == "/mesecni-obracun/?unknown_error=1"
    assert _unknown_ui_snapshot() == before
    if invalid != "outside_step6":
        assert "Odločitve ni bilo mogoče shraniti." in client.get(response["Location"]).content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_audits_once_preserves_data_and_recomputes(
    client, unknown_delivery, monkeypatch, action
):
    _cycle, attempt = unknown_delivery
    before = _unknown_ui_snapshot()
    forbidden = lambda *a, **k: pytest.fail("Resolution must not use SMTP or Keychain")
    for target in (
        "delivery.services.send_pdf_email", "delivery.transport.send_prepared_email",
        "delivery.credentials.load_password", "keyring.get_password",
        "ui.views.resolve_smtp_config", "ui.views.resolve_keyring_credential_ref",
    ):
        monkeypatch.setattr(target, forbidden)
    data = {
        "action": action, "invoice_id": attempt.invoice_id, "attempt_id": attempt.pk,
        "reason": "  Preverjeno pri ponudniku.  ", "status": "POSLAN",
    }
    response = client.post("/mesecni-obracun/", data)
    assert response.status_code == 302 and response["Location"] == "/mesecni-obracun/"
    sent = action == "unknown_sent"
    invoice = Invoice.objects.get(pk=attempt.invoice_id)
    assert invoice.status == (Invoice.Status.POSLAN if sent else Invoice.Status.IZBRAN_ZA_POSILJANJE)
    audit = AuditLogEntry.objects.get(polje=UNKNOWN_RESOLUTION_FIELD)
    assert audit.object_id == attempt.pk
    assert audit.content_type == ContentType.objects.get_for_model(DeliveryAttempt)
    assert audit.dejanje == AuditLogEntry.Dejanje.SPREMEMBA
    assert (audit.stara_vrednost, audit.nova_vrednost, audit.razlog) == (
        UNKNOWN_RESOLUTION_UNRESOLVED,
        UNKNOWN_RESOLUTION_SENT_VERIFIED if sent else UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED,
        "Preverjeno pri ponudniku.",
    )
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == before[DeliveryAttempt][0]
    assert DeliveryAttempt.objects.count() == (1 if sent else 2)
    after = _unknown_ui_snapshot()
    for model in (InvoiceLine, MonthlyCycle, MonthlyBatch, MonthlyCandidateReview, NumberSequence):
        assert after[model] == before[model]
    for row in before[Invoice]:
        if row["id"] == invoice.pk:
            row["status"] = invoice.status
    assert after[Invoice] == before[Invoice]
    if not sent:
        retry = DeliveryAttempt.objects.get(sequence=2)
        assert retry.result == DeliveryAttempt.Result.PREPARED
        for field in ("sender", "recipients_to", "recipients_cc", "recipients_bcc", "pdf_path", "pdf_checksum"):
            assert getattr(retry, field) == getattr(attempt, field)
    refreshed = client.get(response["Location"])
    state = refreshed.context["delivery_state"]
    assert state["unknown_count"] == 0
    assert state["per_invoice"][0]["delivery_state"] == ("DELIVERY_SENT" if sent else "DELIVERY_PREPARED")
    final = refreshed.context["final_control_state"]
    assert final["unknown_count"] == 0
    assert final["delivery_pending_count"] == (0 if sent else 1)
    assert refreshed.context["archive_can_run"] == sent
    assert refreshed.context["delivery_can_send"] != sent
    assert 'value="unknown_sent"' not in refreshed.content.decode()
    stable = _unknown_ui_snapshot()
    for repeated_action in ("unknown_sent", "unknown_not_sent"):
        response = client.post("/mesecni-obracun/", {**data, "action": repeated_action})
        assert response.status_code == 302 and "unknown_error" in response["Location"]
        assert _unknown_ui_snapshot() == stable
    if sent:
        assert client.post("/mesecni-obracun/", {"action": "archive_sent_invoices"}).status_code == 302
        final = client.get("/mesecni-obracun/").context["final_control_state"]
        assert final["all_required_work_successful"] and final["unknown_count"] == 0


@pytest.mark.django_db
def test_unknown_not_sent_retry_uses_later_normal_send(
    client, unknown_delivery, settings, monkeypatch
):
    _cycle, attempt = unknown_delivery
    _smtp_settings(settings)
    calls = []

    def send(prepared, **kwargs):
        calls.append(prepared.pk)
        return mark_accepted(mark_submission_started(prepared), code="SMTP_ACCEPTED")

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    response = client.post("/mesecni-obracun/", {
        "action": "unknown_not_sent", "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Preverjeno: ni poslano.",
    })
    assert response.status_code == 302 and not calls
    retry = DeliveryAttempt.objects.get(sequence=2)
    assert client.post("/mesecni-obracun/", {"action": "send_delivery"}).status_code == 302
    assert calls == [retry.pk]
    assert Invoice.objects.get(pk=attempt.invoice_id).status == Invoice.Status.POSLAN
    assert DeliveryAttempt.objects.get(pk=retry.pk).result == DeliveryAttempt.Result.ACCEPTED
    assert client.get("/mesecni-obracun/").context["delivery_state"]["sent_count"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_stale_after_backend_resolution(client, unknown_delivery, action):
    _cycle, attempt = unknown_delivery
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry(attempt, reason="Earlier decision")
    before = _unknown_ui_snapshot()
    response = client.post("/mesecni-obracun/", {
        "action": action, "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Stale decision",
    })
    assert response.status_code == 302 and "unknown_error" in response["Location"]
    assert _unknown_ui_snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_accepts_valid_csrf_token(unknown_delivery, action):
    _cycle, attempt = unknown_delivery
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.get("/mesecni-obracun/")
    response = csrf_client.post("/mesecni-obracun/", {
        "csrfmiddlewaretoken": csrf_client.cookies["csrftoken"].value,
        "action": action, "invoice_id": attempt.invoice_id,
        "attempt_id": attempt.pk, "reason": "Preverjeno.",
    })
    assert response.status_code == 302 and response["Location"] == "/mesecni-obracun/"
    assert AuditLogEntry.objects.filter(polje=UNKNOWN_RESOLUTION_FIELD).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["unknown_sent", "unknown_not_sent"])
def test_unknown_resolution_without_active_cycle_is_rejected(client, action):
    before = _unknown_ui_snapshot()
    response = client.post("/mesecni-obracun/", {
        "action": action, "invoice_id": 1, "attempt_id": 1, "reason": "Preverjeno.",
    })
    assert response.status_code == 302 and "unknown_error" in response["Location"]
    assert _unknown_ui_snapshot() == before


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

    with delivery_execution_lock(attempt.pk) as acquired:
        assert acquired
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
