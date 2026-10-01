from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, connections, models
from django.db.models.deletion import ProtectedError

from companies.models import NumberSequence, Signatory
from companies.services import (
    fizicno_izbrisi_poslovni_zapis as izbrisi_podjetje,
)
from companies.services import (
    odloci_o_fizicnem_izbrisu as odloci_podjetje,
)
from contracts import services as pogodbe_services
from contracts.models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    PoracunIndeksacije,
    Property,
    RecipientContact,
    Unit,
)
from contracts.services import (
    fizicno_izbrisi_poslovni_zapis as izbrisi_pogodbo,
)
from contracts.services import (
    odloci_o_fizicnem_izbrisu as odloci_pogodbo,
)
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def veriga(pogodba):
    return (
        pogodba.unit.property.company,
        pogodba.unit.property,
        pogodba.unit,
        pogodba.tenant,
        pogodba,
    )


def storitvi(zapis):
    if type(zapis).__name__ == "Company":
        return odloci_podjetje, izbrisi_podjetje
    return odloci_pogodbo, izbrisi_pogodbo


def kode(odlocitev):
    return {razlog.koda for razlog in odlocitev.razlogi}


def nov_racun(pogodba, **spremembe):
    return Invoice.objects.create(
        company=pogodba.unit.property.company,
        lease=pogodba,
        document_type=Invoice.DocumentType.NP,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 8, 1),
        obdobje_do=date(2026, 8, 31),
        **spremembe,
    )


def nova_vrstica(pogodba, postavka, racun):
    return InvoiceLine.objects.create(
        invoice=racun,
        lease=pogodba,
        lease_item=postavka,
        opis="Najemnina",
        osnova=Decimal("100.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("22.00"),
        skupaj=Decimal("122.00"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_rekap_skupina="OBDAVCENO",
    )


def nov_dogodek(postavka):
    return IndexationEvent.objects.create(
        lease_item=postavka,
        datum_uskladitve=date(2026, 2, 1),
        indeks_vrednost=Decimal("101.000"),
        stara_vrednost=Decimal("100.00"),
        nova_vrednost=Decimal("101.00"),
    )


def test_neuporabljeni_zapisi_in_minimalni_audit(pogodba):
    postavka = dodaj_postavko(
        pogodba, naziv="Zasebna najemnina", potrdi_ddv=False
    )
    zapisi = (*veriga(pogodba), postavka)
    podjetje = zapisi[0]
    Signatory.objects.filter(company=podjetje).delete()
    podjetje.ddv_razporeditev_potrjena = False
    podjetje.save(update_fields=["ddv_razporeditev_potrjena"])

    for zapis in reversed(zapisi):
        odlocanje, izbris = storitvi(zapis)
        assert odlocanje(zapis, izrecna_potrditev=True).dovoljeno
        identifikator = zapis.pk
        polje_vsebine = {
            "Company": "naziv",
            "Property": "naziv",
            "Unit": "oznaka",
            "Tenant": "naziv",
            "Lease": "oznaka_pogodbe",
            "LeaseItem": "naziv_postavke",
        }[type(zapis).__name__]
        poslovna_vsebina = getattr(zapis, polje_vsebine)
        assert izbris(zapis, izrecna_potrditev=True).dovoljeno
        assert not type(zapis).objects.filter(pk=identifikator).exists()
        revizija = AuditLogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(type(zapis)),
            object_id=identifikator,
        )
        assert revizija.dejanje == AuditLogEntry.Dejanje.FIZICNI_IZBRIS
        assert revizija.polje == revizija.stara_vrednost == revizija.nova_vrednost == ""
        assert revizija.razlog == (
            "Uporabnik je izrecno potrdil fizični izbris neuporabljenega zapisa."
        )
        assert poslovna_vsebina not in revizija.razlog
        assert poslovna_vsebina not in revizija.stara_vrednost


@pytest.mark.parametrize("potrditev", [False, "true", 1, None])
def test_brez_izrecne_potrditve_ni_izbrisa(pogodba, potrditev):
    postavka = dodaj_postavko(pogodba)
    rezultat = izbrisi_pogodbo(postavka, izrecna_potrditev=potrditev)

    assert kode(rezultat) == {"POTRDITEV_MANJKA"}
    assert rezultat.mozno_arhiviranje
    assert LeaseItem.objects.filter(pk=postavka.pk).exists()
    assert not AuditLogEntry.objects.exists()


@pytest.mark.parametrize(
    ("starsevski_tip", "tip_povezave"),
    [
        ("Company", "Property"),
        ("Company", "Signatory"),
        ("Property", "Unit"),
        ("Unit", "Lease"),
        ("Tenant", "Lease"),
        ("Lease", "LeaseItem"),
        ("Lease", "RecipientContact"),
    ],
)
def test_vsak_otrok_blokira_starsa_brez_kaskade(pogodba, starsevski_tip, tip_povezave):
    postavka = dodaj_postavko(pogodba)
    RecipientContact.objects.create(
        lease=pogodba,
        email="prejemnik@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=RecipientContact.Kanal.PDF,
    )
    zapisi = (*veriga(pogodba), postavka)
    stars = next(z for z in zapisi if type(z).__name__ == starsevski_tip)
    _, izbris = storitvi(stars)
    rezultat = izbris(stars, izrecna_potrditev=True)

    assert not rezultat.dovoljeno
    assert rezultat.mozno_arhiviranje
    assert any(
        r.koda == "PODREJENI_ZAPISI"
        and r.tip_povezave == tip_povezave
        and r.stevilo_povezav >= 1
        for r in rezultat.razlogi
    )
    assert type(stars).objects.filter(pk=stars.pk).exists()
    model = {
        "Property": Property,
        "Signatory": Signatory,
        "Unit": Unit,
        "Lease": Lease,
        "LeaseItem": LeaseItem,
        "RecipientContact": RecipientContact,
    }[tip_povezave]
    assert model.objects.exists()
    assert not AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(type(stars)),
        object_id=stars.pk,
    ).exists()


def test_vse_sočasne_razloge_in_obstojece_reference(pogodba):
    podjetje = pogodba.unit.property.company
    postavka = dodaj_postavko(pogodba)
    NumberSequence.objects.create(company=podjetje, tip=NumberSequence.Tip.NP, leto=0)
    racun = nov_racun(
        pogodba,
        stevilka="NP-00001",
        status=Invoice.Status.POSLAN,
        rendered_pdf_path="racun.pdf",
    )
    nova_vrstica(pogodba, postavka, racun)
    dogodek = nov_dogodek(postavka)
    PoracunIndeksacije.objects.create(
        indexation_event=dogodek,
        original_invoice=racun,
        prizadeto_obdobje_od=date(2026, 8, 1),
        prizadeto_obdobje_do=date(2026, 8, 31),
        znesek_poracuna=Decimal("1.00"),
    )
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(LeaseItem),
        object_id=postavka.pk,
        polje="znesek_neto_mesecno",
        razlog="Sprememba najemnine",
    )
    podjetje_rezultat = odloci_podjetje(podjetje)
    pogodba_rezultat = odloci_pogodbo(pogodba, izrecna_potrditev=True)
    postavka_rezultat = odloci_pogodbo(postavka, izrecna_potrditev=True)

    assert {
        "PODREJENI_ZAPISI",
        "POVEZAN_RACUN",
        "POVEZANO_STEVILCENJE",
        "POVEZANA_POTRDITEV",
        "ZAPIS_UPORABLJEN",
        "POVEZAN_DOKUMENT",
        "POVEZANO_POSILJANJE",
        "POTRDITEV_MANJKA",
    } <= kode(podjetje_rezultat)
    assert {"PODREJENI_ZAPISI", "POVEZAN_RACUN"} <= kode(pogodba_rezultat)
    assert {
        "POVEZAN_RACUN",
        "POSLOVNA_ZGODOVINA",
        "ZAPIS_UPORABLJEN",
        "POVEZAN_DOKUMENT",
        "POVEZANO_POSILJANJE",
    } <= kode(postavka_rezultat)
    assert {r.tip_povezave for r in postavka_rezultat.razlogi} >= {
        "InvoiceLine",
        "IndexationEvent",
        "PoracunIndeksacije",
        "AuditLogEntry",
    }
    assert not izbrisi_podjetje(podjetje, izrecna_potrditev=True).dovoljeno
    assert not izbrisi_pogodbo(postavka, izrecna_potrditev=True).dovoljeno
    assert Invoice.objects.filter(pk=racun.pk).exists()
    assert PoracunIndeksacije.objects.exists()


@pytest.mark.parametrize(
    "tip", ["Company", "Property", "Unit", "Tenant", "Lease", "LeaseItem"]
)
def test_audit_poslovne_zgodovine_blokira_vsak_tip(pogodba, tip):
    postavka = dodaj_postavko(pogodba)
    zapis = next(z for z in (*veriga(pogodba), postavka) if type(z).__name__ == tip)
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(type(zapis)),
        object_id=zapis.pk,
        polje="naziv",
        razlog="Poslovna sprememba",
    )
    odlocanje, izbris = storitvi(zapis)
    rezultat = odlocanje(zapis, izrecna_potrditev=True)
    assert any(
        r.koda == "POSLOVNA_ZGODOVINA" and r.tip_povezave == "AuditLogEntry"
        for r in rezultat.razlogi
    )
    assert not izbris(zapis, izrecna_potrditev=True).dovoljeno
    assert type(zapis).objects.filter(pk=zapis.pk).exists()


def test_ponovno_preverjanje_znotraj_transakcije(pogodba, monkeypatch):
    postavka = dodaj_postavko(pogodba)
    prvotna = pogodbe_services.odloci_o_fizicnem_izbrisu
    klici = 0

    def odlocitev_z_novo_referenco(zapis, *, izrecna_potrditev=False):
        nonlocal klici
        klici += 1
        if klici == 2:
            assert connection.in_atomic_block
            nov_dogodek(postavka)
        return prvotna(zapis, izrecna_potrditev=izrecna_potrditev)

    monkeypatch.setattr(
        pogodbe_services, "odloci_o_fizicnem_izbrisu", odlocitev_z_novo_referenco
    )
    rezultat = izbrisi_pogodbo(postavka, izrecna_potrditev=True)

    assert klici == 2
    assert "POSLOVNA_ZGODOVINA" in kode(rezultat)
    assert LeaseItem.objects.filter(pk=postavka.pk).exists()
    assert IndexationEvent.objects.filter(lease_item=postavka).exists()
    assert not AuditLogEntry.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_socasen_nastanek_reference_varno_prepreci_izbris(pogodba, monkeypatch):
    postavka = dodaj_postavko(pogodba)
    prvotna = pogodbe_services.odloci_o_fizicnem_izbrisu
    prvic_preverjeno = Event()
    referenca_vpisana = Event()

    def pocakaj_po_prvem_preverjanju(zapis, *, izrecna_potrditev=False):
        odlocitev = prvotna(zapis, izrecna_potrditev=izrecna_potrditev)
        if not prvic_preverjeno.is_set():
            assert odlocitev.dovoljeno
            prvic_preverjeno.set()
            assert referenca_vpisana.wait(10)
        return odlocitev

    monkeypatch.setattr(
        pogodbe_services,
        "odloci_o_fizicnem_izbrisu",
        pocakaj_po_prvem_preverjanju,
    )

    def izbrisi_v_niti():
        povezava = connections["default"]
        povezava.close()
        povezava.connect()
        try:
            return (
                izbrisi_pogodbo(postavka, izrecna_potrditev=True),
                id(povezava.connection),
            )
        finally:
            povezava.close()

    def dodaj_referenco_v_niti():
        povezava = connections["default"]
        povezava.close()
        try:
            assert prvic_preverjeno.wait(10)
            povezava.connect()
            identifikator_povezave = id(povezava.connection)
            nov_dogodek(postavka)
            return identifikator_povezave
        finally:
            referenca_vpisana.set()
            povezava.close()

    with ThreadPoolExecutor(max_workers=2) as izvajalec:
        brisanje = izvajalec.submit(izbrisi_v_niti)
        referenca = izvajalec.submit(dodaj_referenco_v_niti)
        druga_povezava = referenca.result(timeout=15)
        odlocitev, prva_povezava = brisanje.result(timeout=15)

    assert prva_povezava != druga_povezava
    assert not odlocitev.dovoljeno
    assert "POSLOVNA_ZGODOVINA" in kode(odlocitev)
    assert LeaseItem.objects.filter(pk=postavka.pk).exists()
    assert IndexationEvent.objects.filter(lease_item_id=postavka.pk).count() == 1
    assert not AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(LeaseItem),
        object_id=postavka.pk,
    ).exists()


def test_protect_povrne_audit_in_vrne_strukturiran_razlog(pogodba, monkeypatch):
    postavka = dodaj_postavko(pogodba)

    def zavrni_izbris(self, *args, **kwargs):
        raise ProtectedError("Nova povezava.", {self})

    monkeypatch.setattr(LeaseItem, "delete", zavrni_izbris)
    rezultat = izbrisi_pogodbo(postavka, izrecna_potrditev=True)

    assert not rezultat.dovoljeno
    assert kode(rezultat) == {"POSLOVNA_POVEZAVA"}
    assert rezultat.mozno_arhiviranje
    assert LeaseItem.objects.filter(pk=postavka.pk).exists()
    assert not AuditLogEntry.objects.exists()


def test_poslovni_tuji_kljuci_so_protect():
    for tip in (
        Property,
        Unit,
        Lease,
        LeaseItem,
        RecipientContact,
        IndexationEvent,
        PoracunIndeksacije,
        Invoice,
        InvoiceLine,
        Signatory,
        NumberSequence,
    ):
        for polje in tip._meta.fields:
            if isinstance(polje, models.ForeignKey):
                assert polje.remote_field.on_delete is models.PROTECT
