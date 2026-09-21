from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from companies.models import NumberSequence
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    dodaj_vrstico,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_np,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_np,
)
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]

E_NASLOV = "elektronski_naslov_kupca_vkljucen"
KONTAKT = "kontaktni_podatki_kupca_vkljuceni"
BASELINE = {
    "version": "eslog-2.0-v200-config-v1",
    "segments": {E_NASLOV: False, KONTAKT: False},
    "fields": {},
}


def test_model_storage_defaults_in_validacija(pogodba):
    company = pogodba.unit.property.company
    tenant = pogodba.tenant
    assert company.eslog_privzeti_neobvezni_segmenti == BASELINE["segments"]
    assert tenant.eslog_neobvezni_segmenti_override == {}
    assert tenant.eslog_neobvezna_polja == {}

    tenant.eslog_neobvezni_segmenti_override = {E_NASLOV: True, KONTAKT: False}
    tenant.eslog_neobvezna_polja = {"elektronski_naslov_kupca": " a@b.si "}
    tenant.full_clean()
    assert tenant.eslog_neobvezna_polja == {
        "elektronski_naslov_kupca": "a@b.si"
    }


@pytest.mark.parametrize(
    ("vrednost", "polje"),
    [
        ({E_NASLOV: False}, "company"),
        ({E_NASLOV: False, KONTAKT: False, "neznan": False}, "company"),
        ({E_NASLOV: 0, KONTAKT: False}, "company"),
        ({E_NASLOV: None}, "tenant_override"),
        ({"neznano": "x"}, "tenant_fields"),
        ({"kontaktni_email_kupca": "ni-email"}, "tenant_fields"),
    ],
)
def test_model_storage_zavrne_neveljavne_vrednosti(pogodba, vrednost, polje):
    if polje == "company":
        objekt = pogodba.unit.property.company
        objekt.eslog_privzeti_neobvezni_segmenti = vrednost
        ime = "eslog_privzeti_neobvezni_segmenti"
    else:
        objekt = pogodba.tenant
        ime = (
            "eslog_neobvezni_segmenti_override"
            if polje == "tenant_override"
            else "eslog_neobvezna_polja"
        )
        setattr(objekt, ime, vrednost)
    with pytest.raises(ValidationError) as napaka:
        objekt.full_clean()
    assert ime in napaka.value.message_dict


def test_np_snapshot_nastane_ob_potrditvi_in_ostane_nespremenjen(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi_osnutek_np(
        pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    assert invoice.eslog_konfiguracija_snapshot is None

    company = pogodba.unit.property.company
    tenant = pogodba.tenant
    company.eslog_privzeti_neobvezni_segmenti = {E_NASLOV: True, KONTAKT: False}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezna_polja = {"elektronski_naslov_kupca": " racuni@x.si "}
    tenant.save(update_fields=["eslog_neobvezna_polja"])

    potrjen = potrdi_osnutek_np(invoice)
    assert potrjen.eslog_konfiguracija_snapshot == {
        "version": "eslog-2.0-v200-config-v1",
        "segments": {E_NASLOV: True, KONTAKT: False},
        "fields": {"elektronski_naslov_kupca": "racuni@x.si"},
    }
    company.eslog_privzeti_neobvezni_segmenti = BASELINE["segments"]
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezna_polja = {}
    tenant.save(update_fields=["eslog_neobvezna_polja"])
    potrjen.refresh_from_db()
    assert potrjen.eslog_konfiguracija_snapshot["segments"][E_NASLOV] is True


def test_neveljavna_np_konfiguracija_ne_porabi_stevilke(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi_osnutek_np(
        pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    company = pogodba.unit.property.company
    company.eslog_privzeti_neobvezni_segmenti = {E_NASLOV: True, KONTAKT: False}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])

    with pytest.raises(ValueError, match="BT-49"):
        potrdi_osnutek_np(invoice)
    invoice.refresh_from_db()
    assert invoice.eslog_konfiguracija_snapshot is None
    assert invoice.stevilka is None
    assert not NumberSequence.objects.exists()


@pytest.mark.parametrize("tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS])
def test_korekcija_uporabi_trenutno_konfiguracijo_ob_lastni_potrditvi(
    pogodba, tip
):
    dodaj_postavko(pogodba)
    original = potrdi_osnutek_np(
        pripravi_osnutek_np(
            pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
        ).invoice
    )
    assert original.eslog_konfiguracija_snapshot == BASELINE
    popravek = pripravi_korekcijski_osnutek(
        original, tip, "Napaka", date(2026, 9, 2)
    )
    assert popravek.eslog_konfiguracija_snapshot is None
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    company = pogodba.unit.property.company
    tenant = pogodba.tenant
    company.eslog_privzeti_neobvezni_segmenti = {E_NASLOV: False, KONTAKT: True}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezna_polja = {"kontaktna_tocka_kupca": "Nabava"}
    tenant.save(update_fields=["eslog_neobvezna_polja"])

    potrjen = potrdi_korekcijski_osnutek(popravek)
    assert potrjen.eslog_konfiguracija_snapshot["segments"][KONTAKT] is True
    assert potrjen.eslog_konfiguracija_snapshot["fields"] == {
        "kontaktna_tocka_kupca": "Nabava"
    }
    original.refresh_from_db()
    assert original.eslog_konfiguracija_snapshot == BASELINE

    tenant.eslog_neobvezna_polja = {"kontaktna_tocka_kupca": "Drugo"}
    tenant.save(update_fields=["eslog_neobvezna_polja"])
    potrjen.refresh_from_db()
    assert potrjen.eslog_konfiguracija_snapshot["fields"] == {
        "kontaktna_tocka_kupca": "Nabava"
    }


@pytest.mark.parametrize("tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS])
def test_neveljavna_korekcijska_konfiguracija_ne_porabi_stevilke(pogodba, tip):
    dodaj_postavko(pogodba)
    original = potrdi_osnutek_np(
        pripravi_osnutek_np(
            pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
        ).invoice
    )
    popravek = pripravi_korekcijski_osnutek(
        original, tip, "Napaka", date(2026, 9, 2)
    )
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-1.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-1.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    company = pogodba.unit.property.company
    company.eslog_privzeti_neobvezni_segmenti = {E_NASLOV: True, KONTAKT: False}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    zaporedje = NumberSequence.objects.get()

    with pytest.raises(ValueError, match="BT-49"):
        potrdi_korekcijski_osnutek(popravek)
    zaporedje.refresh_from_db()
    popravek.refresh_from_db()
    assert zaporedje.zadnja_stevilka == 1
    assert popravek.stevilka is None
    assert popravek.eslog_konfiguracija_snapshot is None


def test_korekcija_brez_pogodbe_odpove_pred_stevilcenjem(pogodba):
    dodaj_postavko(pogodba)
    original = potrdi_osnutek_np(
        pripravi_osnutek_np(
            pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
        ).invoice
    )
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    Invoice.objects.filter(pk=storno.pk).update(lease=None)

    with pytest.raises(ValueError, match="nima pogodbe.*eSLOG konfiguracije"):
        potrdi_korekcijski_osnutek(storno)

    storno.refresh_from_db()
    assert storno.stevilka is None
    assert storno.eslog_konfiguracija_snapshot is None
    assert NumberSequence.objects.get(tip=NumberSequence.Tip.NP).zadnja_stevilka == 1


def test_lease_st_korekcija_uporabi_trenutno_konfiguracijo(pogodba):
    dodaj_postavko(pogodba)
    np = potrdi_osnutek_np(
        pripravi_osnutek_np(
            pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
        ).invoice
    )
    original = Invoice.objects.create(
        company=np.company,
        lease=np.lease,
        document_type=Invoice.DocumentType.ST,
        stevilka="ST-2026-0001",
        status=Invoice.Status.POSLAN,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=np.obdobje_od,
        obdobje_do=np.obdobje_do,
        izdajatelj_podpisnik_slika_pot=np.izdajatelj_podpisnik_slika_pot,
        eslog_konfiguracija_snapshot=BASELINE,
    )
    NumberSequence.objects.create(
        company=np.company,
        tip=NumberSequence.Tip.ST,
        leto=2026,
        zadnja_stevilka=1,
    )
    InvoiceLine.objects.create(
        invoice=original,
        opis="Storitev",
        osnova=Decimal("20.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("4.40"),
        skupaj=Decimal("24.40"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_eslog_koda="S",
        ddv_rekap_skupina="OBDAVCENO",
    )
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2027, 1, 2)
    )
    company = pogodba.unit.property.company
    tenant = pogodba.tenant
    company.eslog_privzeti_neobvezni_segmenti = {E_NASLOV: False, KONTAKT: True}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezna_polja = {"kontaktna_tocka_kupca": "Nabava"}
    tenant.save(update_fields=["eslog_neobvezna_polja"])

    potrjen = potrdi_korekcijski_osnutek(storno)

    assert potrjen.eslog_konfiguracija_snapshot == {
        "version": "eslog-2.0-v200-config-v1",
        "segments": {E_NASLOV: False, KONTAKT: True},
        "fields": {"kontaktna_tocka_kupca": "Nabava"},
    }
    original.refresh_from_db()
    assert original.eslog_konfiguracija_snapshot == BASELINE
