from copy import deepcopy
from datetime import date
from itertools import product

import pytest
from django.core.exceptions import ValidationError

from catalogs.models import VatRegime
from companies.models import NumberSequence
from companies.services import (
    potrdi_ddv_nastavitve,
    potrdi_vzorec_podjetja,
    veljavna_potrditev_ddv,
)
from contracts.models import Lease, Tenant, Unit
from contracts.services import izracunaj_postavko
from contracts.vat_eligibility import preveri_upravicenost_prejemnika
from invoicing.models import Invoice, InvoiceLine, MonthlyCandidateReview
from invoicing.services import (
    _candidate_fingerprint,
    _preveri_izdajni_snapshot,
    izdaj_zamrznjeni_mesecni_paket,
    materializiraj_zamrznjenega_mesecnega_kandidata,
    potrdi_osnutek_np,
    preveri_predhodno_kontrolo,
    pripravi_osnutek_np,
    spremeni_vrstico,
)
from tests.catalogs.test_models import podatki_rezima
from tests.invoicing.test_preflight import (
    _calculate,
    _freeze_ready_package,
)
from tests.invoicing.test_preflight import (
    ready_company as ready_company,  # noqa: PLC0414 -- pytest fixture re-export
)
from tests.ui.test_catalog_settings import vat_data
from tests.ui.test_contract_leases import item_data, lease_data
from ui.forms import LeaseForm, LeaseItemCreateForm, LeaseItemDetailsForm, VatRegimeForm


@pytest.mark.parametrize("tip,registered,full", list(product(
    ["PRAVNA_OSEBA", "FIZICNA_OSEBA"], [False, True], [False, True]
)))
@pytest.mark.parametrize("restricted", [False, True])
def test_recipient_eligibility_matrix(tip, registered, full, restricted):
    arguments = {
        "sifra": "CUSTOM", "zahteva_polni_odbitek": restricted,
        "tip": tip, "identificiran_za_ddv": registered,
        "polni_odbitek": full, "najemnik": "Prejemnik", "postavka": "Najem",
    }
    if restricted and not (tip == "PRAVNA_OSEBA" and registered and full):
        with pytest.raises(ValidationError, match="Prejemnik.*Najem.*CUSTOM"):
            preveri_upravicenost_prejemnika(**arguments)
    else:
        preveri_upravicenost_prejemnika(**arguments)


@pytest.mark.parametrize("restriction", [None, "false", 0, 1])
def test_unknown_or_malformed_restriction_fails_closed(restriction):
    with pytest.raises(ValidationError):
        preveri_upravicenost_prejemnika(
            sifra="CUSTOM", zahteva_polni_odbitek=restriction,
            tip="PRAVNA_OSEBA", identificiran_za_ddv=True, polni_odbitek=True,
            najemnik="Prejemnik", postavka="Najem",
        )


@pytest.mark.parametrize("code,restriction", [("OBDAVCENO_45", False), ("OBDAVCENO_STANDARD", True)])
def test_canonical_regime_rejects_inconsistent_restriction(code, restriction):
    with pytest.raises(ValidationError):
        preveri_upravicenost_prejemnika(
            sifra=code, zahteva_polni_odbitek=restriction,
            tip="PRAVNA_OSEBA", identificiran_za_ddv=True, polni_odbitek=True,
            najemnik="Prejemnik", postavka="Najem",
        )


@pytest.mark.django_db
@pytest.mark.parametrize("code,expected", [("OBDAVCENO_45", True), ("OBDAVCENO_STANDARD", False)])
@pytest.mark.parametrize("valid", [False, True])
def test_canonical_configuration_model_and_form(code, expected, valid):
    restriction = expected if valid else not expected
    regime = VatRegime(**podatki_rezima(sifra=code, zahteva_polni_odbitek=restriction))
    form = VatRegimeForm(vat_data(sifra=code, zahteva_polni_odbitek=str(restriction)))
    assert form.is_valid() is valid
    if valid:
        regime.full_clean()
        assert form.save().zahteva_polni_odbitek is expected
    else:
        assert "zahteva_polni_odbitek" in form.errors
        with pytest.raises(ValidationError) as exc:
            regime.full_clean()
        assert "zahteva_polni_odbitek" in exc.value.message_dict


@pytest.mark.parametrize("code,restriction", [("OBDAVCENO_45", False), ("OBDAVCENO_STANDARD", True)])
def test_calculation_rejects_inconsistent_canonical_configuration(ready_company, code, restriction):
    data = ready_company
    _set_eligibility(data)
    VatRegime.objects.filter(pk=data["regime"].pk).update(
        sifra=code, zahteva_polni_odbitek=restriction
    )
    data["item"].refresh_from_db()
    with pytest.raises(ValidationError, match=code):
        izracunaj_postavko(data["item"], date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.django_db
@pytest.mark.parametrize("code,restriction", [("OBDAVCENO_45", False), ("OBDAVCENO_STANDARD", True)])
def test_catalog_ui_rejects_inconsistent_canonical_configuration(client, code, restriction):
    response = client.post(
        "/nastavitve/ddv/nov/",
        vat_data(sifra=code, zahteva_polni_odbitek=str(restriction)),
    )
    assert response.status_code == 200
    assert "zahteva_polni_odbitek" in response.context["form"].errors
    assert not VatRegime.objects.filter(sifra=code).exists()


def _set_eligibility(data, *, restricted=True, tip="PRAVNA_OSEBA", registered=True, full=True):
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_polni_odbitek=restricted)
    Tenant.objects.filter(pk=data["tenant"].pk).update(
        tip=tip, identificiran_za_ddv=registered, ddv_zavezanec_s_polnim_odbitkom=full
    )
    for key in ("tenant", "regime", "lease", "item"):
        data[key].refresh_from_db()
    potrdi_ddv_nastavitve(data["company"])
    if restricted is False or (restricted is True and tip == "PRAVNA_OSEBA" and registered and full):
        potrdi_vzorec_podjetja(data["company"], data["lease"])


@pytest.mark.parametrize("tip,registered,full", list(product(
    ["PRAVNA_OSEBA", "FIZICNA_OSEBA"], [False, True], [False, True]
)))
def test_assignment_and_preflight_matrix(ready_company, tip, registered, full):
    data = ready_company
    _set_eligibility(data, tip=tip, registered=registered, full=full)
    eligible = tip == "PRAVNA_OSEBA" and registered and full
    create = LeaseItemCreateForm(item_data(data["regime"]), lease=data["lease"])
    edit = LeaseItemDetailsForm(item_data(data["regime"]), instance=data["item"])
    assert create.is_valid() is eligible
    assert edit.is_valid() is eligible
    if not eligible:
        assert "vat_regime" in create.errors and "vat_regime" in edit.errors
        with pytest.raises(ValidationError):
            data["item"].full_clean()
        with pytest.raises(ValidationError):
            izracunaj_postavko(data["item"], date(2026, 9, 1), date(2026, 9, 30))
    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    issues = [i for i in data["batch"].preflight_issues if i["code"] == "DDV_PREJEMNIK_NEUPRAVICEN"]
    assert bool(issues) is not eligible
    if issues:
        assert issues[0]["object_id"] == data["item"].pk
        assert data["tenant"].naziv in issues[0]["message"]
        assert data["regime"].sifra in issues[0]["message"]
    assert not Invoice.objects.exists() and not NumberSequence.objects.exists()


@pytest.mark.parametrize("unit_type", [Unit.TipEnote.PARKIRNO_MESTO, Unit.TipEnote.GARAZA, Unit.TipEnote.OPREMA])
def test_standard_taxable_items_allow_ineligible_recipient(ready_company, unit_type):
    data = ready_company
    _set_eligibility(data, restricted=False, tip="FIZICNA_OSEBA", registered=False, full=False)
    VatRegime.objects.filter(pk=data["regime"].pk).update(sifra="OBDAVCENO_STANDARD")
    Unit.objects.filter(pk=data["unit"].pk).update(tip_enote=unit_type)
    data["item"].refresh_from_db()
    data["item"].full_clean()
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    candidate, _review = _freeze_ready_package(data)
    result = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    assert result["ok"]
    assert candidate["lines"][0]["requires_full_deduction"] is False
    assert Invoice.objects.get().ddv_skupaj == 22


def test_lease_reassignment_cannot_bypass_restriction(ready_company):
    data = ready_company
    _set_eligibility(data)
    other = Tenant.objects.create(
        tip="FIZICNA_OSEBA", naziv="Drug prejemnik", naslov="A", drzava_koda="SI",
        identificiran_za_ddv=False, email="other@example.com",
    )
    form = LeaseForm(lease_data(data["lease"], tenant=other.pk), instance=data["lease"])
    assert not form.is_valid() and "tenant" in form.errors
    data["lease"].refresh_from_db()
    data["lease"].tenant = other
    with pytest.raises(ValidationError):
        data["lease"].full_clean()
    assert Lease.objects.get(pk=data["lease"].pk).tenant_id == data["tenant"].pk


@pytest.mark.django_db
@pytest.mark.parametrize("choice", [None, "", "on", "0", "garbage"])
def test_custom_regime_requires_explicit_choice(choice):
    data = vat_data()
    if choice is None:
        data.pop("zahteva_polni_odbitek")
    else:
        data["zahteva_polni_odbitek"] = choice
    form = VatRegimeForm(data)
    assert not form.is_valid() and "zahteva_polni_odbitek" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize("choice,expected", [("True", True), ("False", False)])
def test_custom_regime_explicit_yes_no_is_saved(choice, expected):
    form = VatRegimeForm(vat_data(zahteva_polni_odbitek=choice))
    assert form.is_valid(), form.errors
    assert form.save().zahteva_polni_odbitek is expected


def test_reference_requirement_is_independent(ready_company):
    data = ready_company
    _set_eligibility(data, restricted=False, registered=False, full=False)
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_referenco=True)
    data["item"].refresh_from_db()
    with pytest.raises(ValidationError) as exc:
        data["item"].full_clean()
    assert "referenca_listine" in exc.value.message_dict
    assert "vat_regime" not in exc.value.message_dict


def test_unknown_legacy_regime_blocks_assignment_and_preflight(ready_company):
    data = ready_company
    _set_eligibility(data, restricted=None)
    with pytest.raises(ValidationError):
        data["item"].full_clean()
    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    assert "DDV_PREJEMNIK_NEUPRAVICEN" in {i["code"] for i in data["batch"].preflight_issues}


@pytest.mark.parametrize("damage", ["live_status", "snapshot_status", "missing_evidence", "restriction_unknown", "article45_false", "standard_true"])
def test_direct_issuance_rejects_before_numbering(ready_company, damage):
    data = ready_company
    _set_eligibility(data)
    invoice = pripravi_osnutek_np(data["lease"], date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10)).invoice
    if damage == "live_status":
        Tenant.objects.filter(pk=data["tenant"].pk).update(ddv_zavezanec_s_polnim_odbitkom=False)
    elif damage in {"snapshot_status", "missing_evidence"}:
        Invoice.objects.filter(pk=invoice.pk).update(prejemnik_polni_odbitek=False if damage == "snapshot_status" else None)
    elif damage in {"article45_false", "standard_true"}:
        InvoiceLine.objects.filter(invoice=invoice).update(
            ddv_rezim_sifra="OBDAVCENO_45" if damage == "article45_false" else "OBDAVCENO_STANDARD",
            ddv_zahteva_polni_odbitek=damage == "standard_true",
        )
    else:
        InvoiceLine.objects.filter(invoice=invoice).update(ddv_zahteva_polni_odbitek=None)
    with pytest.raises(ValidationError):
        potrdi_osnutek_np(invoice)
    invoice.refresh_from_db()
    assert invoice.stevilka is None
    assert not NumberSequence.objects.exists()


def _replace_frozen(data, candidate):
    batch = data["batch"]
    batch.refresh_from_db()
    snapshot = deepcopy(batch.calculation_snapshot)
    old_fingerprint = candidate["candidate_fingerprint"]
    changed = snapshot["candidates"][0]
    changed.update(candidate)
    source = dict(changed)
    source.pop("candidate_fingerprint", None)
    changed["candidate_fingerprint"] = _candidate_fingerprint(
        source, company=snapshot["company"], period_start=snapshot["period_start"],
        period_end=snapshot["period_end"], issue_date=snapshot["issue_date"],
        schema_version=snapshot["schema_version"],
    )
    MonthlyCandidateReview.objects.filter(batch=batch, candidate_fingerprint=old_fingerprint).update(
        candidate_fingerprint=changed["candidate_fingerprint"]
    )
    batch.calculation_snapshot = snapshot
    batch.save(update_fields=["calculation_snapshot"])
    return changed


@pytest.mark.parametrize("damage", ["individual", "unregistered", "no_deduction", "missing_deduction", "missing_restriction", "malformed_deduction", "malformed_restriction", "article45_false", "standard_true"])
def test_frozen_invalid_evidence_rejects_before_numbering(ready_company, damage):
    data = ready_company
    _set_eligibility(data)
    candidate, _review = _freeze_ready_package(data)
    candidate = deepcopy(candidate)
    tenant, line = candidate["tenant"], candidate["lines"][0]
    if damage == "individual":
        tenant["type"] = "FIZICNA_OSEBA"
    elif damage == "unregistered":
        tenant["vat_registered"] = False
    elif damage == "no_deduction":
        tenant["full_deduction"] = False
    elif damage == "missing_deduction":
        tenant.pop("full_deduction")
    elif damage == "missing_restriction":
        line.pop("requires_full_deduction")
    elif damage == "malformed_deduction":
        tenant["full_deduction"] = "True"
    elif damage == "malformed_restriction":
        line["requires_full_deduction"] = 1
    elif damage == "standard_true":
        line.update(vat_regime_code="OBDAVCENO_STANDARD", requires_full_deduction=True)
    else:
        line.update(vat_regime_code="OBDAVCENO_45", requires_full_deduction=False)
    candidate = _replace_frozen(data, candidate)
    with pytest.raises(ValidationError):
        materializiraj_zamrznjenega_mesecnega_kandidata(data["batch"].pk, data["lease"].pk, candidate["candidate_fingerprint"])
    assert not NumberSequence.objects.exists()
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


def test_frozen_evidence_is_immutable_and_retry_consumes_no_number(ready_company):
    data = ready_company
    _set_eligibility(data)
    candidate, _review = _freeze_ready_package(data)
    Tenant.objects.filter(pk=data["tenant"].pk).update(ddv_zavezanec_s_polnim_odbitkom=False)
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_polni_odbitek=None)
    first = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    second = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    assert first["ok"] and second["ok"] and second["reused"]
    invoice = Invoice.objects.get()
    line = InvoiceLine.objects.get()
    assert invoice.prejemnik_polni_odbitek is True
    assert line.ddv_zahteva_polni_odbitek is True
    assert line.ddv_rezim_sifra == candidate["lines"][0]["vat_regime_code"]
    assert NumberSequence.objects.get().zadnja_stevilka == 1
    with pytest.raises(ValueError):
        spremeni_vrstico(line, ddv_zahteva_polni_odbitek=False)


def test_eligibility_change_changes_candidate_and_confirmation_fingerprints(ready_company):
    data = ready_company
    before = _calculate(data)[0]["candidate_fingerprint"]
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_polni_odbitek=True)
    Tenant.objects.filter(pk=data["tenant"].pk).update(ddv_zavezanec_s_polnim_odbitkom=True)
    assert veljavna_potrditev_ddv(data["company"]).stanje == "stale"
    potrdi_ddv_nastavitve(data["company"])
    data["lease"].refresh_from_db()
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    after = _calculate(data)[0]["candidate_fingerprint"]
    assert before != after


@pytest.mark.parametrize("issued", [False, True])
def test_legacy_frozen_snapshot_only_allows_exact_issued_retry(ready_company, issued):
    data = ready_company
    candidate, _review = _freeze_ready_package(data)
    if issued:
        assert izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["ok"]
        Invoice.objects.update(prejemnik_polni_odbitek=None)
        InvoiceLine.objects.update(ddv_rezim_sifra="", ddv_zahteva_polni_odbitek=None)
    data["batch"].refresh_from_db()
    snapshot = deepcopy(data["batch"].calculation_snapshot)
    snapshot["schema_version"] = 3
    data["batch"].calculation_snapshot = snapshot
    data["batch"].calculation_schema_version = 3
    data["batch"].save(update_fields=["calculation_snapshot", "calculation_schema_version"])
    candidate = deepcopy(candidate)
    candidate["tenant"].pop("full_deduction")
    for line in candidate["lines"]:
        line.pop("requires_full_deduction")
        line.pop("vat_regime_code")
    _replace_frozen(data, candidate)
    result = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    assert result["ok"] is issued
    if issued:
        assert result["reused"]
        assert Invoice.objects.get().prejemnik_polni_odbitek is None
        assert InvoiceLine.objects.get().ddv_zahteva_polni_odbitek is None
        assert NumberSequence.objects.get().zadnja_stevilka == 1
    else:
        assert not Invoice.objects.exists() and not NumberSequence.objects.exists()


def test_direct_issuance_persists_eligibility_evidence(ready_company):
    data = ready_company
    _set_eligibility(data)
    invoice = pripravi_osnutek_np(data["lease"], date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10)).invoice
    issued = potrdi_osnutek_np(invoice)
    line = issued.invoiceline_set.get()
    assert issued.prejemnik_polni_odbitek is True
    assert line.ddv_zahteva_polni_odbitek is True
    assert line.ddv_rezim_sifra == data["regime"].sifra
    assert NumberSequence.objects.get().zadnja_stevilka == 1


def test_rejection_preserves_existing_sequence_and_valid_retry(ready_company):
    data = ready_company
    _set_eligibility(data)
    candidate, _review = _freeze_ready_package(data)
    sequence = NumberSequence.objects.create(
        company=data["company"], tip=NumberSequence.Tip.NP, leto=0, zadnja_stevilka=41
    )
    invalid = deepcopy(candidate)
    invalid["tenant"]["full_deduction"] = False
    invalid = _replace_frozen(data, invalid)
    result = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    assert not result["ok"]
    sequence.refresh_from_db()
    assert sequence.zadnja_stevilka == 41
    assert not Invoice.objects.exists()
    candidate["candidate_fingerprint"] = invalid["candidate_fingerprint"]
    _replace_frozen(data, candidate)
    assert izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["ok"]
    assert Invoice.objects.get().stevilka == "NP-00042"
    assert izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["reused"]
    sequence.refresh_from_db()
    assert sequence.zadnja_stevilka == 42


def test_invalid_eligibility_blocks_freeze(ready_company):
    data = ready_company
    _set_eligibility(data)
    candidate = deepcopy(_calculate(data)[0])
    candidate["tenant"]["full_deduction"] = False
    with pytest.raises(ValidationError):
        _preveri_izdajni_snapshot(candidate)
    assert not NumberSequence.objects.exists()
