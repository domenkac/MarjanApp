from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from companies.models import NumberSequence
from contracts.models import Lease
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    potrdi_osnutek_np,
    primerjaj_delta_np,
    pripravi_osnutek_np,
    uvozi_migracijsko_stanje,
)
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def test_uvoz_ustvari_sinteticni_snapshot_brez_nove_stevilke(pogodba):
    prva = dodaj_postavko(pogodba, znesek="100.00")
    druga = dodaj_postavko(
        pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO"
    )
    koncana = dodaj_postavko(
        pogodba, naziv="Končana", datum_prenehanja_postavke=date(2026, 6, 30)
    )
    zaporedje = NumberSequence.objects.create(
        company=pogodba.unit.property.company,
        tip=NumberSequence.Tip.NP,
        leto=0,
        zadnja_stevilka=41,
    )

    migracija = uvozi_migracijsko_stanje(pogodba, date(2026, 7, 1), date(2026, 7, 31))

    assert migracija.company_id == pogodba.unit.property.company_id
    assert migracija.document_type == Invoice.DocumentType.NP
    assert migracija.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert migracija.razlog_arhiviranja == (
        "Migracijski zapis – uvoz stanja pred prehodom na aplikacijo"
    )
    assert migracija.stevilka is None
    assert (migracija.datum_izdaje, migracija.obdobje_od, migracija.obdobje_do) == (
        date(2026, 7, 1),
        date(2026, 7, 1),
        date(2026, 7, 31),
    )
    assert (migracija.skupaj_z_ddv, migracija.rok_placila) == (None, None)
    vrstice = {
        vrstica.lease_item_id: vrstica
        for vrstica in InvoiceLine.objects.filter(invoice=migracija)
    }
    assert set(vrstice) == {prva.pk, druga.pk}
    assert koncana.pk not in vrstice
    assert (
        vrstice[prva.pk].osnova,
        vrstice[prva.pk].ddv_znesek,
        vrstice[prva.pk].skupaj,
    ) == (Decimal("100.00"), Decimal("22.00"), Decimal("122.00"))
    assert (
        vrstice[druga.pk].osnova,
        vrstice[druga.pk].ddv_znesek,
        vrstice[druga.pk].skupaj,
    ) == (Decimal("30.00"), Decimal("0.00"), Decimal("30.00"))
    assert vrstice[prva.pk].ddv_nacin_obracuna == "OBDAVCENO"
    assert vrstice[druga.pk].ddv_nacin_obracuna == "OPROSCENO"
    zaporedje.refresh_from_db()
    assert zaporedje.zadnja_stevilka == 41


def test_ponovni_uvoz_je_zavrnjen_tudi_za_drugo_obdobje(pogodba):
    dodaj_postavko(pogodba)
    uvozi_migracijsko_stanje(pogodba, date(2026, 1, 1), date(2026, 1, 31))

    with pytest.raises(ValueError, match="že uvoženo"):
        uvozi_migracijsko_stanje(pogodba, date(2026, 2, 1), date(2026, 2, 28))

    assert Invoice.objects.count() == 1
    assert InvoiceLine.objects.count() == 1


def test_prvi_np_uporabi_migracijsko_stanje_in_nato_pravi_racun(pogodba):
    dodaj_postavko(pogodba)
    migracija = uvozi_migracijsko_stanje(pogodba, date(2026, 1, 1), date(2026, 1, 31))
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 1)
    ).invoice
    primerjava = primerjaj_delta_np(osnutek)
    assert primerjava.prejsnji_racun_id == migracija.pk
    assert not primerjava.obstaja_razlika
    assert {r.koda for r in primerjava.razlage} == {"BREZ_RAZLIKE"}

    izdani = potrdi_osnutek_np(osnutek)
    naslednji = pripravi_osnutek_np(
        pogodba, date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 1)
    ).invoice
    assert primerjaj_delta_np(naslednji).prejsnji_racun_id == izdani.pk


def test_delta_pravilno_primerja_dve_postavki_z_migracijo(pogodba):
    prva = dodaj_postavko(pogodba, znesek="100.00")
    druga = dodaj_postavko(
        pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO"
    )
    migracija = uvozi_migracijsko_stanje(pogodba, date(2026, 1, 1), date(2026, 1, 31))
    prva.znesek_neto_mesecno = Decimal("110.00")
    prva.save(update_fields=["znesek_neto_mesecno"])
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 1)
    ).invoice

    primerjava = primerjaj_delta_np(osnutek)
    assert primerjava.prejsnji_racun_id == migracija.pk
    assert primerjava.obstaja_razlika
    assert [(r.koda, r.lease_item_id) for r in primerjava.razlage] == [
        ("VZROK_NEZNAN", prva.pk)
    ]
    assert {
        v.lease_item_id: v.osnova for v in InvoiceLine.objects.filter(invoice=migracija)
    } == {
        prva.pk: Decimal("100.00"),
        druga.pk: Decimal("30.00"),
    }


def test_obicajen_arhiviran_osnutek_ni_migracijska_referenca(pogodba):
    dodaj_postavko(pogodba)
    Invoice.objects.create(
        company=pogodba.unit.property.company,
        lease=pogodba,
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
        razlog_arhiviranja="Ročno arhiviran osnutek",
        datum_izdaje=date(2026, 1, 1),
        obdobje_od=date(2026, 1, 1),
        obdobje_do=date(2026, 1, 31),
    )
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 1)
    ).invoice
    assert {r.koda for r in primerjaj_delta_np(osnutek).razlage} == {"PRVA_IZDAJA"}


def test_obicajen_arhiviran_osnutek_ne_preglasi_migracije(pogodba):
    dodaj_postavko(pogodba)
    migracija = uvozi_migracijsko_stanje(pogodba, date(2026, 1, 1), date(2026, 1, 31))
    Invoice.objects.create(
        company=pogodba.unit.property.company,
        lease=pogodba,
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
        razlog_arhiviranja="Rocno arhiviran osnutek",
        datum_izdaje=date(2026, 2, 1),
        obdobje_od=date(2026, 2, 1),
        obdobje_do=date(2026, 2, 28),
    )
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 1)
    ).invoice
    assert primerjaj_delta_np(osnutek).prejsnji_racun_id == migracija.pk


def test_uvoz_ohrani_indeksacijsko_osnovo_iz_referencnega_obdobja(pogodba):
    pogodba.ima_klavzulo_indeksacije = True
    pogodba.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    pogodba.save(update_fields=["ima_klavzulo_indeksacije", "tip_pogoja_indeksacije"])
    item = dodaj_postavko(
        pogodba,
        indeks_ob_osnovi=Decimal("123.456"),
        datum_osnove_indeksacije=date(2026, 7, 15),
    )

    uvozi_migracijsko_stanje(pogodba, date(2026, 7, 1), date(2026, 7, 31))

    item.refresh_from_db()
    assert (item.indeks_ob_osnovi, item.datum_osnove_indeksacije) == (
        Decimal("123.456"),
        date(2026, 7, 15),
    )


def test_uvoz_zavrne_manjkajoco_obvezno_indeksacijsko_osnovo(pogodba):
    pogodba.ima_klavzulo_indeksacije = True
    pogodba.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    pogodba.save(update_fields=["ima_klavzulo_indeksacije", "tip_pogoja_indeksacije"])
    dodaj_postavko(pogodba)

    with pytest.raises(ValidationError):
        uvozi_migracijsko_stanje(pogodba, date(2026, 7, 1), date(2026, 7, 31))

    assert not Invoice.objects.exists()


def test_uvoz_rollbacka_racun_ce_vrstice_ne_uspejo(pogodba):
    dodaj_postavko(pogodba)
    with (
        patch.object(
            InvoiceLine.objects, "bulk_create", side_effect=RuntimeError("napaka")
        ),
        pytest.raises(RuntimeError, match="napaka"),
    ):
        uvozi_migracijsko_stanje(pogodba, date(2026, 1, 1), date(2026, 1, 31))

    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()
    assert not NumberSequence.objects.exists()
