from decimal import Decimal

from companies.models import NumberSequence, Signatory
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    potrdi_osnutek_np,
    primerjaj_delta_np,
    pripravi_osnutek_np,
)
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_priprava_np import (
    DATUM_IZDAJE,
    OBDOBJE_DO,
    OBDOBJE_OD,
    dodaj_postavko,
)
from tests.setup_helpers import prepare_initial_setup

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def test_faza1_pot_od_pogodbe_do_izdanega_posnetka(pogodba):
    company = pogodba.unit.property.company
    property = pogodba.unit.property
    unit = pogodba.unit
    tenant = pogodba.tenant
    podpisnik = Signatory.objects.get(company=company)
    obdavcena = dodaj_postavko(pogodba, znesek="100.00")
    oproscena = dodaj_postavko(
        pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO"
    )

    osnutek = pripravi_osnutek_np(pogodba, OBDOBJE_OD, OBDOBJE_DO, DATUM_IZDAJE).invoice
    assert osnutek is not None
    assert (osnutek.company, osnutek.lease, osnutek.status, osnutek.stevilka) == (
        company, pogodba, Invoice.Status.OSNUTEK, None
    )
    vrstice = {v.lease_item_id: v for v in InvoiceLine.objects.filter(invoice=osnutek)}
    assert set(vrstice) == {obdavcena.pk, oproscena.pk}
    assert [
        (vrstice[item.pk].osnova, vrstice[item.pk].ddv_znesek)
        for item in (obdavcena, oproscena)
    ] == [(Decimal("100.00"), Decimal("22.00")), (Decimal("30.00"), Decimal("0.00"))]
    delta = primerjaj_delta_np(osnutek)
    assert (delta.ima_prejsnji_racun, delta.obstaja_razlika) == (False, False)
    assert [razlaga.koda for razlaga in delta.razlage] == ["PRVA_IZDAJA"]

    prepare_initial_setup(osnutek.company, osnutek.lease)
    potrjen = potrdi_osnutek_np(osnutek)
    assert (potrjen.stevilka, potrjen.status, potrjen.rok_placila) == (
        "NP-00001", Invoice.Status.IZBRAN_ZA_POSILJANJE, DATUM_IZDAJE.replace(day=16)
    )
    assert (potrjen.osnova_skupaj, potrjen.ddv_skupaj, potrjen.skupaj_z_ddv) == (
        Decimal("130.00"), Decimal("22.00"), Decimal("152.00")
    )
    assert NumberSequence.objects.get(company=company, tip="NP", leto=0).zadnja_stevilka == 1
    posnetek_racuna = (
        potrjen.prejemnik_naziv, potrjen.izdajatelj_naziv,
        potrjen.izdajatelj_podpisnik_ime, potrjen.oznaka_pogodbe,
        potrjen.objekt_naziv, potrjen.skupaj_z_ddv,
    )
    posnetek_vrstic = {
        item_id: (v.opis, v.osnova, v.ddv_stopnja, v.ddv_znesek)
        for item_id, v in vrstice.items()
    }

    tenant.naziv = "Nov najemnik"
    save_business_record(tenant, update_fields=["naziv"])
    company.naziv = "Nov izdajatelj"
    save_financial_fixture(company, update_fields=["naziv"])
    podpisnik.ime_priimek = "Nov podpisnik"
    save_business_record(podpisnik, update_fields=["ime_priimek"])
    property.naziv = "Nov objekt"
    save_business_record(property, update_fields=["naziv"])
    unit.oznaka = "Nova enota"
    save_business_record(unit, update_fields=["oznaka"])
    pogodba.oznaka_pogodbe = "Nova oznaka"
    save_financial_fixture(pogodba, update_fields=["oznaka_pogodbe"])
    obdavcena.znesek_neto_mesecno = Decimal("200.00")
    save_financial_fixture(obdavcena, update_fields=["znesek_neto_mesecno"])
    obdavcena.vat_regime.stopnja = Decimal("9.50")
    save_financial_fixture(obdavcena.vat_regime, update_fields=["stopnja"])

    potrjen.refresh_from_db()
    assert (
        potrjen.prejemnik_naziv, potrjen.izdajatelj_naziv,
        potrjen.izdajatelj_podpisnik_ime, potrjen.oznaka_pogodbe,
        potrjen.objekt_naziv, potrjen.skupaj_z_ddv,
    ) == posnetek_racuna
    assert {
        v.lease_item_id: (v.opis, v.osnova, v.ddv_stopnja, v.ddv_znesek)
        for v in InvoiceLine.objects.filter(invoice=potrjen)
    } == posnetek_vrstic
