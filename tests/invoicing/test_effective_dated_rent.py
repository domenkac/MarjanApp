from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from companies.models import NumberSequence
from companies.services import potrdi_ddv_nastavitve
from contracts.models import Lease, LeaseItem
from contracts.services import potrdi_indeksacijo, razveljavi_indeksacijo
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch
from invoicing.services import (
    izdaj_zamrznjeni_mesecni_paket,
    potrdi_osnutek_np,
    preveri_predhodno_kontrolo,
    pripravi_osnutek_np,
)
from tests.invoicing.test_preflight import (  # noqa: F401
    _freeze_ready_package,
    potrdi_septembrski_vzorec,
    ready_company,
)
from tests.invoicing.test_priprava_np import dodaj_postavko, pogodba  # noqa: F401
from tests.setup_helpers import approve_sample, prepare_initial_setup


def indeksirana(lease):
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    lease.save()
    return dodaj_postavko(
        lease, indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(2026, 1, 1),
    )


def test_pravi_osnutki_in_izdani_posnetki_ostanejo(pogodba):  # noqa: F811
    item = indeksirana(pogodba)
    prepare_initial_setup(pogodba.unit.property.company, pogodba)
    januar = potrdi_osnutek_np(pripravi_osnutek_np(pogodba, date(2026, 1, 1), date(2026, 1, 31), date(2026, 1, 1)).invoice)
    prepare_initial_setup(pogodba.unit.property.company, pogodba)
    februar = potrdi_osnutek_np(pripravi_osnutek_np(pogodba, date(2026, 2, 1), date(2026, 2, 28), date(2026, 2, 1)).invoice)
    januar.rendered_pdf_path = "original.pdf"
    januar.checksum = "original-checksum"
    januar.save()
    before = (Invoice.objects.values().get(pk=januar.pk), Invoice.objects.values().get(pk=februar.pk), list(InvoiceLine.objects.values()))
    potrdi_indeksacijo(item, Decimal("110.000"), date(2027, 3, 15), indeks_leto=2025)
    januar.refresh_from_db()
    februar.refresh_from_db()
    assert Invoice.objects.values().get(pk=januar.pk) == before[0]
    assert Invoice.objects.values().get(pk=februar.pk) == before[1]
    assert list(InvoiceLine.objects.values()) == before[2]
    for month, end, amount in [(1, 31, "100.00"), (2, 28, "110.00")]:
        draft = pripravi_osnutek_np(pogodba, date(2026, month, 1), date(2026, month, end), date(2026, month, 1)).invoice
        assert InvoiceLine.objects.get(invoice=draft).osnova == Decimal(amount)
    prepare_initial_setup(pogodba.unit.property.company, pogodba)
    approve_sample(pogodba.unit.property.company, pogodba)
    marec = potrdi_osnutek_np(pripravi_osnutek_np(pogodba, date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 1)).invoice)
    assert marec.stevilka == "NP-00003"


def test_zastarel_zamrznjen_paket_blokira_izdajo_in_ohrani_vsebino(ready_company, potrdi_septembrski_vzorec):  # noqa: F811
    data = ready_company
    item = data["item"]
    lease = data["lease"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    lease.save()
    item.indeks_ob_osnovi = Decimal("100.000")
    item.datum_osnove_indeksacije = date(2026, 1, 1)
    item.save()
    potrdi_ddv_nastavitve(data["company"])
    potrdi_septembrski_vzorec(data["company"], lease)
    _freeze_ready_package(data)
    before = list(MonthlyBatch.objects.values("calculation_snapshot", "calculation_schema_version"))
    event = potrdi_indeksacijo(item, Decimal("110.000"), date(2026, 3, 15), indeks_leto=2025)
    assert list(MonthlyBatch.objects.values("calculation_snapshot", "calculation_schema_version")) == before
    with pytest.raises(ValidationError, match="zamrznjenem"):
        razveljavi_indeksacijo(event)
    first = izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    assert all(not result["ok"] for result in first)
    assert "zastarel" in first[0]["error"]
    lines = list(InvoiceLine.objects.values())
    retry = izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    assert all(not result["ok"] for result in retry)
    assert not Invoice.objects.exists() and not NumberSequence.objects.filter(zadnja_stevilka__gt=0).exists()
    assert list(InvoiceLine.objects.values()) == lines
    assert list(MonthlyBatch.objects.values("calculation_snapshot", "calculation_schema_version")) == before


def test_veljaven_indeksiran_zamrznjen_paket_ohrani_uspesno_izdajo_in_retry(
    ready_company, potrdi_septembrski_vzorec,  # noqa: F811
):
    data = ready_company
    item, lease = data["item"], data["lease"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    lease.save()
    item.indeks_ob_osnovi = Decimal("100.000")
    item.datum_osnove_indeksacije = date(2026, 1, 1)
    item.save()
    potrdi_indeksacijo(item, Decimal("110.000"), date(2026, 3, 15), indeks_leto=2025)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_septembrski_vzorec(data["company"], lease)
    _freeze_ready_package(data)
    before = list(MonthlyBatch.objects.values("calculation_snapshot", "calculation_schema_version"))
    first = izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    assert all(result["ok"] and not result["reused"] for result in first)
    assert first[0]["invoice"].osnova_skupaj == Decimal("110.00")
    invoices, lines = list(Invoice.objects.values()), list(InvoiceLine.objects.values())
    retry = izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    assert all(result["ok"] and result["reused"] for result in retry)
    assert list(Invoice.objects.values()) == invoices
    assert list(InvoiceLine.objects.values()) == lines
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == 1
    assert list(MonthlyBatch.objects.values("calculation_snapshot", "calculation_schema_version")) == before


def test_legacy_reference_reported_as_preflight_blocker(ready_company):  # noqa: F811
    data = ready_company
    lease = data["lease"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    lease.save()
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        indeks_ob_osnovi=Decimal("100.000"), datum_osnove_indeksacije=date(2026, 1, 1),
        referencna_najemnina=None, datum_referencne_najemnine=None,
    )
    batch = preveri_predhodno_kontrolo(data["cycle"])[0]
    assert batch.preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    issue = next(item for item in batch.preflight_issues if item["code"] == "NAJEMNINA_ZGODOVINA_NERAZJASNJENA")
    assert issue["object_id"] == data["item"].pk
    assert "nerazjasnjena" in issue["message"]
    assert not Invoice.objects.exists()
