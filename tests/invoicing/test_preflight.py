import base64
from datetime import date
from decimal import Decimal

import pytest

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from companies.services import potrdi_ddv_nastavitve, potrdi_vzorec_podjetja
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from invoicing.services import preveri_predhodno_kontrolo

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def ready_company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(PNG)
    (tmp_path / "signature.png").write_bytes(PNG)
    company = Company.objects.create(
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        drzava_koda="SI",
        trr="SI56191000000123438",
        logotip="logo.png",
        ddv_zavezanec=True,
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="alfa",
        ddv_razporeditev_potrjena=True,
    )
    signatory = Signatory.objects.create(
        company=company,
        ime_priimek="Ana Podpisnica",
        funkcija="Direktorica",
        podpis_slika="signature.png",
        velja_od=date(2026, 1, 1),
    )
    property_ = Property.objects.create(
        company=company, naziv="Stavba A", naslov="Glavna 1"
    )
    unit = Unit.objects.create(
        property=property_, oznaka="P-1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        drzava_koda="SI",
        identificiran_za_ddv=True,
        email="najemnik@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-2026",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    regime = VatRegime.objects.create(
        sifra="R22",
        naziv="DDV 22",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="DDV 22 %",
        eslog_koda="S",
        velja_od=date(2026, 1, 1),
    )
    item = LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=regime,
    )
    potrdi_ddv_nastavitve(company)
    potrdi_vzorec_podjetja(company, lease)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 10),
        trenutni_korak=MonthlyCycle.Korak.PREDHODNA_KONTROLA,
    )
    batch = MonthlyBatch.objects.create(cycle=cycle, company=company)
    return {
        "company": company,
        "signatory": signatory,
        "property": property_,
        "unit": unit,
        "tenant": tenant,
        "lease": lease,
        "regime": regime,
        "item": item,
        "cycle": cycle,
        "batch": batch,
    }


def _codes(batch):
    batch.refresh_from_db()
    return {issue["code"] for issue in batch.preflight_issues}


def _add_ready_company(data):
    company = Company.objects.create(
        naziv="Beta d.o.o.", davcna_stevilka="12345679",
        maticna_stevilka="1234567001", naslov="Druga 2", drzava_koda="SI",
        trr="SI56191000000123438", logotip="logo.png",
        email_racunovodja="beta@example.com", email_posiljatelj="beta@example.com",
        keychain_kljuc_posiljatelja="beta", ddv_razporeditev_potrjena=True,
    )
    Signatory.objects.create(
        company=company, ime_priimek="Berta Podpisnica",
        podpis_slika="signature.png", velja_od=date(2026, 1, 1),
    )
    property_ = Property.objects.create(company=company, naziv="Stavba B", naslov="Druga 2")
    unit = Unit.objects.create(property=property_, oznaka="B-1", tip_enote=Unit.TipEnote.PISARNA)
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA, naziv="Beta najemnik",
        davcna_stevilka="12345679", maticna_stevilka="1234567001",
        naslov="Trg 2", drzava_koda="SI", identificiran_za_ddv=False,
        email="beta@example.com",
    )
    lease = Lease.objects.create(
        unit=unit, tenant=tenant, oznaka_pogodbe="B-2026",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1), rok_placila_dni=15,
    )
    LeaseItem.objects.create(
        lease=lease, znesek_neto_mesecno=Decimal("80.00"),
        vat_regime=data["regime"],
    )
    potrdi_ddv_nastavitve(company)
    potrdi_vzorec_podjetja(company, lease)
    return company, MonthlyBatch.objects.create(cycle=data["cycle"], company=company)


def test_batch_defaults_to_not_checked(db):
    assert MonthlyBatch._meta.get_field("preflight_status").get_default() == "NOT_CHECKED"
    assert MonthlyBatch._meta.get_field("preflight_issues").get_default() == []


def test_valid_company_passes_and_rerun_replaces_snapshot(ready_company):
    data = ready_company
    before_sequences = list(NumberSequence.objects.values_list("pk", "zadnja_stevilka"))
    assert not (Invoice.objects.exists() or InvoiceLine.objects.exists())

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    first_checked = data["batch"].preflight_checked_at
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED

    Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)
    preveri_predhodno_kontrolo(data["cycle"])
    assert "DDV_POTRDITEV_ZASTARELA" in _codes(data["batch"])
    data["batch"].refresh_from_db()
    assert data["batch"].preflight_checked_at > first_checked
    assert not (Invoice.objects.exists() or InvoiceLine.objects.exists())
    assert list(NumberSequence.objects.values_list("pk", "zadnja_stevilka")) == before_sequences


def test_stale_sample_blocks(ready_company):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        znesek_neto_mesecno=Decimal("110.00")
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "VZOREC_POTRDITEV_ZASTARELA" in _codes(data["batch"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trr", "SI00NEVELJAVEN"),
        ("davcna_stevilka", "00000000"),
        ("maticna_stevilka", "123"),
        ("drzava_koda", "XX"),
    ],
)
def test_invalid_company_master_data_blocks(ready_company, field, value):
    data = ready_company
    Company.objects.filter(pk=data["company"].pk).update(**{field: value})

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert any(
        issue["code"] == "PODJETJE_NEVELJAVNO" and issue["field"] == field
        for issue in data["batch"].preflight_issues
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"identificiran_za_ddv": False, "davcna_stevilka": ""}, "davcna_stevilka"),
        (
            {"drzava_koda": "AT", "identificiran_za_ddv": True,
             "ddv_identifikacijska_stevilka": ""},
            "ddv_identifikacijska_stevilka",
        ),
        ({"davcna_stevilka": "00000000"}, "davcna_stevilka"),
    ],
)
def test_invalid_tenant_business_data_blocks(ready_company, changes, field):
    data = ready_company
    Tenant.objects.filter(pk=data["tenant"].pk).update(**changes)

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert any(
        issue["code"] == "NAJEMNIK_NEVELJAVEN" and issue["field"] == field
        for issue in data["batch"].preflight_issues
    )


def test_malformed_company_is_isolated_from_valid_company(ready_company):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    valid_batch.refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    assert valid_batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED, valid_batch.preflight_issues


@pytest.mark.parametrize("stale", [False, True])
def test_vat_problem_blocks_only_affected_company(ready_company, stale):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    if stale:
        Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)
        expected = "DDV_POTRDITEV_ZASTARELA"
    else:
        data["company"].vat_settings_confirmations.all().delete()
        expected = "DDV_POTRDITEV_MANJKA"

    preveri_predhodno_kontrolo(data["cycle"])
    valid_batch.refresh_from_db()

    assert expected in _codes(data["batch"])
    assert valid_batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED, valid_batch.preflight_issues


@pytest.mark.parametrize(
    ("model", "change", "expected"),
    [
        ("vat", None, "DDV_POTRDITEV_MANJKA"),
        ("sample", None, "VZOREC_POTRDITEV_MANJKA"),
        ("signatory", None, "PODPISNIK_MANJKA"),
    ],
)
def test_missing_company_requirements_block(ready_company, model, change, expected):
    data = ready_company
    if model == "vat":
        data["company"].vat_settings_confirmations.all().delete()
    elif model == "sample":
        data["company"].sample_confirmations.all().delete()
    else:
        data["signatory"].delete()

    preveri_predhodno_kontrolo(data["cycle"])

    assert expected in _codes(data["batch"])
    data["batch"].refresh_from_db()
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED


def test_overlapping_signatories_block(ready_company):
    data = ready_company
    data["signatory"].velja_do = date(2026, 12, 31)
    data["signatory"].save(update_fields=["velja_do"])
    Signatory.objects.create(
        company=data["company"],
        ime_priimek="Drugi Podpisnik",
        podpis_slika="signature.png",
        velja_od=date(2026, 9, 1),
        velja_do=date(2026, 10, 1),
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "PODPISNIK_PREKRIVANJE" in _codes(data["batch"])


def test_required_reference_fields_block(ready_company):
    data = ready_company
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_referenco=True)

    preveri_predhodno_kontrolo(data["cycle"])

    assert {"REFERENCA_LISTINE_MANJKA", "DATUM_REFERENCE_MANJKA"} <= _codes(
        data["batch"]
    )


def test_valid_required_reference_passes(ready_company):
    data = ready_company
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_referenco=True)
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        referenca_listine="POG-1", datum_reference=date(2026, 1, 1)
    )
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])

    preveri_predhodno_kontrolo(data["cycle"])

    assert "REFERENCA_LISTINE_MANJKA" not in _codes(data["batch"])
    assert "DATUM_REFERENCE_MANJKA" not in _codes(data["batch"])


@pytest.mark.parametrize(
    ("start", "end", "included"),
    [
        (date(2026, 9, 1), None, True),
        (date(2026, 1, 1), date(2026, 9, 30), True),
        (date(2026, 1, 1), date(2026, 8, 31), False),
        (date(2026, 10, 1), None, False),
    ],
)
def test_lease_month_scope(ready_company, start, end, included):
    data = ready_company
    Lease.objects.filter(pk=data["lease"].pk).update(
        datum_zacetka=start,
        tip_trajanja=(
            Lease.TipTrajanja.DOLOCEN_CAS if end else Lease.TipTrajanja.NEDOLOCEN_CAS
        ),
        datum_konca=end,
    )
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert ("POSTAVKA_NEVELJAVNA" in _codes(data["batch"])) is included


def test_proportional_and_expiry_are_warnings_only(ready_company):
    data = ready_company
    Lease.objects.filter(pk=data["lease"].pk).update(
        datum_zacetka=date(2026, 9, 10),
        tip_trajanja=Lease.TipTrajanja.DOLOCEN_CAS,
        datum_konca=date(2026, 9, 30),
    )
    potrdi_vzorec_podjetja(data["company"], data["lease"])

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert {"SORAZMERNO_OBDOBJE_POGODBE", "POGODBA_POTECE_V_MESECU"} <= _codes(
        data["batch"]
    )
    assert all(issue["severity"] == "WARNING" for issue in data["batch"].preflight_issues)


def test_inactive_chain_is_excluded(ready_company):
    data = ready_company
    Unit.objects.filter(pk=data["unit"].pk).update(aktivna=False)
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert "POSTAVKA_NEVELJAVNA" not in _codes(data["batch"])


@pytest.mark.parametrize(("model", "field"), [(Property, "aktivna"), (Unit, "aktivna"), (Tenant, "aktiven")])
def test_each_inactive_chain_member_is_excluded(ready_company, model, field):
    data = ready_company
    key = {Property: "property", Unit: "unit", Tenant: "tenant"}[model]
    model.objects.filter(pk=data[key].pk).update(**{field: False})
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert "POSTAVKA_NEVELJAVNA" not in _codes(data["batch"])


@pytest.mark.parametrize(
    ("start", "end", "included"),
    [
        (date(2026, 9, 1), None, True),
        (None, date(2026, 9, 30), True),
        (None, date(2026, 8, 31), False),
        (date(2026, 10, 1), None, False),
    ],
)
def test_lease_item_month_boundaries(ready_company, start, end, included):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        datum_zacetka_postavke=start,
        datum_prenehanja_postavke=end,
        znesek_neto_mesecno=0,
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert ("POSTAVKA_NEVELJAVNA" in _codes(data["batch"])) is included


def test_company_issue_is_deduplicated_across_multiple_leases(ready_company):
    data = ready_company
    second = Lease.objects.create(
        unit=data["unit"], tenant=data["tenant"], oznaka_pogodbe="P-2",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1), rok_placila_dni=15,
    )
    LeaseItem.objects.create(
        lease=second, znesek_neto_mesecno=Decimal("50.00"), vat_regime=data["regime"]
    )
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    matches = [
        issue for issue in data["batch"].preflight_issues
        if issue["code"] == "PODJETJE_NEVELJAVNO" and issue["field"] == "trr"
    ]
    assert len(matches) == 1


def test_rerun_replaces_old_issues(ready_company):
    data = ready_company
    MonthlyBatch.objects.filter(pk=data["batch"].pk).update(
        preflight_issues=[{
            "code": "OLD", "severity": "BLOCKER", "message": "Old",
            "model": "Company", "object_id": data["company"].pk, "field": None,
        }]
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "OLD" not in _codes(data["batch"])


def test_ui_shows_precise_contract_location_and_persists_for_fresh_client(
    ready_company, client
):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)
    preveri_predhodno_kontrolo(data["cycle"])

    from django.test import Client

    content = Client().get("/mesecni-obracun/").content.decode()
    data["batch"].refresh_from_db()
    amount_issue = next(
        issue
        for issue in data["batch"].preflight_issues
        if issue["field"] == "znesek_neto_mesecno"
    )

    assert "Pogodbe → P-2026" in content
    assert amount_issue["message"] in content


def test_step3_rerun_with_zero_passed_is_truthful(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)

    response = client.post(
        "/mesecni-obracun/", {"action": "rerun_preflight"}, follow=True
    )
    content = response.content.decode()

    assert MonthlyCycle.objects.get().trenutni_korak == 3
    assert "Po ponovnem preverjanju trenutno ni uspešnih podjetij." in content
    assert "zaključena za najmanj eno podjetje" not in content
