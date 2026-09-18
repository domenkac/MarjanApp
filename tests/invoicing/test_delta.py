from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from contracts.models import IndexationEvent, LeaseItem
from contracts.services import rocno_popravi_postavko
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    izracunaj_kandidata,
    potrdi_osnutek_np,
    primerjaj_delta_np,
    pripravi_osnutek_np,
)
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def racun(pogodba, mesec, *, potrdi=False):
    obdobje_od = date(2026, mesec, 1)
    datum_izdaje = date(2026, mesec + 1, 1)
    invoice = pripravi_osnutek_np(
        pogodba, obdobje_od, datum_izdaje - timedelta(days=1), datum_izdaje
    ).invoice
    return potrdi_osnutek_np(invoice) if potrdi else invoice


def kode(rezultat):
    return {razlaga.koda for razlaga in rezultat.razlage}


def test_prva_izdaja_izjema(pogodba):
    dodaj_postavko(pogodba)
    rezultat = primerjaj_delta_np(racun(pogodba, 1))
    assert (rezultat.ima_prejsnji_racun, rezultat.obstaja_razlika, rezultat.pojasnjena) == (
        False, False, None
    )
    assert kode(rezultat) == {"PRVA_IZDAJA"}


def test_znesek_pojasnjen_z_indeksacijo(pogodba):
    item = dodaj_postavko(pogodba)
    prejsnji = racun(pogodba, 1, potrdi=True)
    IndexationEvent.objects.create(
        lease_item=item, datum_uskladitve=date(2026, 2, 1),
        indeks_vrednost=Decimal("110.000"), stara_vrednost=Decimal("100.00"),
        nova_vrednost=Decimal("110.00"),
    )
    item.znesek_neto_mesecno = Decimal("110.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.prejsnji_racun_id == prejsnji.pk
    assert (rezultat.obstaja_razlika, rezultat.pojasnjena) == (True, True)
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("INDEKSACIJA", item.pk)
    ]


def test_znesek_pojasnjen_z_novo_postavko(pogodba):
    dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    nova = dodaj_postavko(
        pogodba, naziv="Parkirišče", znesek="30.00",
        datum_zacetka_postavke=date(2026, 2, 1),
    )
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is True
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("NOVA_POSTAVKA", nova.pk)
    ]


def test_znesek_pojasnjen_z_rocnim_popravkom(pogodba):
    item = dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    rocno_popravi_postavko(
        item, razlog="Ročni popravek", znesek_neto_mesecno=Decimal("120.00")
    )
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is True
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("ROCNI_POPRAVEK", item.pk)
    ]


def test_stara_revizija_ne_pojasni_razlike(pogodba):
    item = dodaj_postavko(pogodba, znesek="100.00", nacin="OPROSCENO")
    prejsnji = racun(pogodba, 1, potrdi=True)
    assert prejsnji.skupaj_z_ddv == Decimal("100.00")

    revizija = AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(LeaseItem),
        object_id=item.pk,
        polje="znesek_neto_mesecno",
        stara_vrednost="100.00",
        nova_vrednost="120.00",
        razlog="Stara sprememba",
    )
    AuditLogEntry.objects.filter(pk=revizija.pk).update(
        created_at=datetime(2026, 1, 31, 12, tzinfo=UTC)
    )
    revizija.refresh_from_db()
    assert revizija.created_at.date() < prejsnji.datum_izdaje

    item.znesek_neto_mesecno = Decimal("120.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    osnutek = racun(pogodba, 2)
    assert InvoiceLine.objects.get(invoice=osnutek).skupaj == Decimal("120.00")

    rezultat = primerjaj_delta_np(osnutek)
    assert rezultat.prejsnji_racun_id == prejsnji.pk
    assert (rezultat.obstaja_razlika, rezultat.pojasnjena) == (True, False)
    assert [(razlaga.koda, razlaga.lease_item_id) for razlaga in rezultat.razlage] == [
        ("VZROK_NEZNAN", item.pk)
    ]

def test_znesek_brez_pojasnila(pogodba):
    item = dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    item.znesek_neto_mesecno = Decimal("120.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    osnutek = racun(pogodba, 2)
    rezultat = primerjaj_delta_np(osnutek)
    assert (rezultat.obstaja_razlika, rezultat.pojasnjena) == (True, False)
    assert rezultat.razlage[0].opis == "vzrok neznan – preveri ročno"
    assert (osnutek.stevilka, osnutek.status, osnutek.skupaj_z_ddv) == (
        None, Invoice.Status.OSNUTEK, None
    )


def test_preskocen_mesec_primerja_z_zadnjim_izdanim(pogodba):
    item = dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    item.znesek_neto_mesecno = Decimal("110.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    prejsnji = racun(pogodba, 2, potrdi=True)
    rezultat = primerjaj_delta_np(racun(pogodba, 4))
    assert rezultat.prejsnji_racun_id == prejsnji.pk
    assert (rezultat.obstaja_razlika, rezultat.pojasnjena) == (False, None)


def test_preskocen_mesec_sprozi_izjemo_tudi_brez_spremembe_zneska(pogodba):
    dodaj_postavko(pogodba)
    prejsnji = racun(pogodba, 1, potrdi=True)
    osnutek = racun(pogodba, 3)

    primerjava = primerjaj_delta_np(osnutek)
    assert primerjava.prejsnji_racun_id == prejsnji.pk
    assert primerjava.obstaja_razlika is False
    assert "PREJSNJI_MESEC_NI_IZDAN" in kode(primerjava)

    kandidat = izracunaj_kandidata(
        pogodba, date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 1)
    )
    assert "PREJSNJI_MESEC_NI_IZDAN" in {
        izjema.koda for izjema in kandidat.izjeme
    }


def test_dve_postavki_razlaga_zadene_pravo(pogodba):
    prva = dodaj_postavko(pogodba)
    druga = dodaj_postavko(pogodba, naziv="Parkirišče", znesek="30.00")
    racun(pogodba, 1, potrdi=True)
    IndexationEvent.objects.create(
        lease_item=prva, datum_uskladitve=date(2026, 2, 1),
        indeks_vrednost=Decimal("110.000"), stara_vrednost=Decimal("100.00"),
        nova_vrednost=Decimal("110.00"),
    )
    druga.znesek_neto_mesecno = Decimal("40.00")
    druga.save(update_fields=["znesek_neto_mesecno"])
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is False
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("VZROK_NEZNAN", druga.pk)
    ]


def test_neostevilcen_osnutek_ni_referenca(pogodba):
    item = dodaj_postavko(pogodba)
    prejsnji = racun(pogodba, 1, potrdi=True)
    item.znesek_neto_mesecno = Decimal("120.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    racun(pogodba, 2)
    item.znesek_neto_mesecno = Decimal("100.00")
    item.save(update_fields=["znesek_neto_mesecno"])
    rezultat = primerjaj_delta_np(racun(pogodba, 3))
    assert rezultat.prejsnji_racun_id == prejsnji.pk
    assert (rezultat.obstaja_razlika, rezultat.pojasnjena) == (False, None)
    assert kode(rezultat) == {"BREZ_RAZLIKE", "PREJSNJI_MESEC_NI_IZDAN"}


def test_prenehanje_postavke_pojasni_razliko(pogodba):
    dodaj_postavko(pogodba)
    koncana = dodaj_postavko(pogodba, naziv="Parkirišče", znesek="30.00")
    racun(pogodba, 1, potrdi=True)
    koncana.datum_prenehanja_postavke = date(2026, 1, 31)
    koncana.save(update_fields=["datum_prenehanja_postavke"])
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is True
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("PRENEHANJE_POSTAVKE", koncana.pk)
    ]


def test_sorazmernost_ne_podvoji_delta_razlage(pogodba):
    item = dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    item.datum_prenehanja_postavke = date(2026, 2, 15)
    item.save(update_fields=["datum_prenehanja_postavke"])
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is True
    assert rezultat.razlage == ()


def test_sprememba_ddv_pojasni_razliko(pogodba):
    item = dodaj_postavko(pogodba)
    racun(pogodba, 1, potrdi=True)
    item.vat_regime.stopnja = Decimal("9.50")
    item.vat_regime.save(update_fields=["stopnja"])
    rezultat = primerjaj_delta_np(racun(pogodba, 2))
    assert rezultat.pojasnjena is True
    assert [(r.koda, r.lease_item_id) for r in rezultat.razlage] == [
        ("SPREMEMBA_DDV", item.pk)
    ]
