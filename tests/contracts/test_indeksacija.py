from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts import services
from contracts.models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    PoracunIndeksacije,
    Property,
    Tenant,
    Unit,
)
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import potrdi_korekcijski_osnutek, pripravi_korekcijski_osnutek

LETO = date.today().year - 1  # noqa: DTZ011
DANES = date(LETO, 3, 15)


@pytest.fixture
def postavka(db):
    company = Company.objects.create(
        drzava_koda="SI",
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
    )
    property = Property.objects.create(
        company=company, naziv="Poslovna stavba", naslov="Glavna ulica 1"
    )
    unit = Unit.objects.create(
        property=property, oznaka="Pisarna", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        drzava_koda="SI",
        identificiran_za_ddv=True,
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        email="najemnik@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-1",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(LETO - 1, 1, 1),
        rok_placila_dni=15,
        ima_klavzulo_indeksacije=True,
        tip_pogoja_indeksacije=Lease.TipPogojaIndeksacije.VEDNO,
    )
    rezim = VatRegime.objects.create(
        sifra="DDV22",
        naziv="Splošna stopnja",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="STANDARD",
        velja_od=date(LETO - 1, 1, 1),
    )
    return LeaseItem.objects.create(
        lease=lease,
        znesek_neto_mesecno=Decimal("100.05"),
        vat_regime=rezim,
        indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(LETO - 1, 1, 1),
    )


def racun_s_postavkami(
    postavka, *druge, leto=LETO, mesec=2, status=Invoice.Status.POSLAN
):
    invoice = Invoice.objects.create(
        company=postavka.lease.unit.property.company,
        lease=postavka.lease,
        document_type=Invoice.DocumentType.NP,
        stevilka=(
            f"NP-{Invoice.objects.count() + 1}"
            if status in {Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN}
            else None
        ),
        datum_izdaje=date(leto, mesec, 1),
        obdobje_od=date(leto, mesec, 1),
        obdobje_do=date(leto + (mesec == 12), mesec % 12 + 1, 1) - timedelta(days=1),
        status=status,
    )
    for item in (postavka, *druge):
        InvoiceLine.objects.create(
            invoice=invoice,
            lease=postavka.lease,
            lease_item=item,
            opis=item.naziv_postavke,
            osnova=item.znesek_neto_mesecno,
            ddv_stopnja=Decimal("22.00"),
            ddv_znesek=Decimal("22.01"),
            skupaj=item.znesek_neto_mesecno + Decimal("22.01"),
            ddv_nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
            ddv_eslog_koda="S",
            ddv_rekap_skupina="STANDARD",
        )
    return invoice


@pytest.mark.parametrize(
    ("tip", "nov_indeks", "prag", "izpolnjen", "razlog"),
    [
        (Lease.TipPogojaIndeksacije.VEDNO, "110.000", None, True, None),
        (Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE, "101.000", "2.00", False, "pragom"),
        (Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE, "103.000", "2.00", True, None),
        (Lease.TipPogojaIndeksacije.SAMO_NAVZGOR, "90.000", None, False, "samo"),
        (Lease.TipPogojaIndeksacije.SAMO_NAVZGOR, "110.000", None, True, None),
    ],
)
def test_predlog_pogoji_brez_spremembe_postavke(
    postavka, tip, nov_indeks, prag, izpolnjen, razlog
):
    lease = postavka.lease
    lease.tip_pogoja_indeksacije = tip
    lease.prag_spremembe_odstotek = Decimal(prag) if prag else None
    lease.save(update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"])

    predlog = services.izracunaj_indeksacijo(postavka, Decimal(nov_indeks), DANES)

    assert predlog.pogoj_izpolnjen is izpolnjen
    assert predlog.razlog
    if razlog:
        assert razlog in predlog.razlog
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()


def test_izracun_zaokrozi_half_up_in_verizno_primerja(postavka):
    predlog = services.izracunaj_indeksacijo(postavka, Decimal("110.000"), DANES)
    assert predlog.stara_vrednost == Decimal("100.05")
    assert predlog.nova_vrednost == Decimal("110.06")
    assert predlog.odstotek_spremembe == Decimal("10.0")

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    postavka.refresh_from_db()
    naslednji = services.izracunaj_indeksacijo(postavka, Decimal("121.000"), DANES)
    assert naslednji.stara_vrednost == Decimal("110.06")
    assert naslednji.nova_vrednost == Decimal("121.07")
    assert naslednji.odstotek_spremembe == Decimal("10.0")


def test_potrditev_shrani_zgodovino_in_spremeni_postavko(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    postavka.refresh_from_db()
    event.refresh_from_db()

    assert event.lease_item == postavka
    assert event.datum_uskladitve == date(LETO, 2, 1)
    assert event.indeks_vrednost == Decimal("110.000")
    assert (event.stara_vrednost, event.nova_vrednost) == (
        Decimal("100.05"),
        Decimal("110.06"),
    )
    assert postavka.znesek_neto_mesecno == Decimal("110.06")


def test_neizpolnjen_pogoj_ne_ustvari_dogodka(postavka):
    postavka.lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    postavka.lease.prag_spremembe_odstotek = Decimal("2.00")
    postavka.lease.save(
        update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"]
    )

    with pytest.raises(ValueError):
        services.potrdi_indeksacijo(postavka, Decimal("101.000"), DANES)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()


def test_potrditev_ob_napaki_razveljavi_dogodek_in_znesek(postavka, monkeypatch):
    def napaka(_event):
        raise RuntimeError("napaka zaznave")

    monkeypatch.setattr(services, "zaznaj_zamujen_poracun", napaka)
    with pytest.raises(RuntimeError, match="napaka zaznave"):
        services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()
    assert not PoracunIndeksacije.objects.exists()


@pytest.mark.parametrize(
    "status", [Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN]
)
def test_zamujen_poracun_samo_za_pravo_postavko(postavka, status):
    druga = LeaseItem.objects.create(
        lease=postavka.lease,
        naziv_postavke="Parkiranje",
        znesek_neto_mesecno=Decimal("25.00"),
        vat_regime=postavka.vat_regime,
        indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(LETO - 1, 1, 1),
    )
    original = racun_s_postavkami(postavka, druga, status=status)
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    poracun = PoracunIndeksacije.objects.get(indexation_event=event)
    poracun.full_clean()
    assert poracun.original_invoice == original
    assert (poracun.prizadeto_obdobje_od, poracun.prizadeto_obdobje_do) == (
        original.obdobje_od,
        original.obdobje_do,
    )
    assert poracun.znesek_poracuna == Decimal("10.01")
    assert poracun.status == PoracunIndeksacije.Status.PREDLAGAN
    assert InvoiceLine.objects.filter(invoice=original).count() == 2
    assert services.zaznaj_zamujen_poracun(event) == []


def test_storno_vrstica_ne_podvoji_poracuna_istega_meseca(postavka, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(b"logo")
    (tmp_path / "podpis.png").write_bytes(b"podpis")
    original = racun_s_postavkami(postavka)
    Signatory.objects.create(
        company=original.company,
        ime_priimek="Ana Podpisnica",
        funkcija="Direktorica",
        podpis_slika="podpis.png",
        velja_od=date(LETO, 1, 1),
    )
    original.izdajatelj_podpisnik_slika_pot = "podpis.png"
    original.save(update_fields=["izdajatelj_podpisnik_slika_pot"])
    NumberSequence.objects.create(
        company=original.company,
        tip=NumberSequence.Tip.NP,
        leto=0,
        zadnja_stevilka=1,
    )
    storno = pripravi_korekcijski_osnutek(
        original,
        Invoice.DocumentType.STORNO,
        "Napaka",
        date(LETO, 4, 1),
    )
    potrdi_korekcijski_osnutek(storno)

    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(LETO, 4, 15))
    poracuni = list(PoracunIndeksacije.objects.filter(indexation_event=event))
    assert len(poracuni) == 1
    assert poracuni[0].original_invoice_id == original.pk


def test_zamuda_vec_mesecev_ustvari_locena_poracuna(postavka):
    februar = racun_s_postavkami(postavka, mesec=2)
    marec = racun_s_postavkami(postavka, mesec=3)
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(LETO, 4, 15))

    assert set(
        PoracunIndeksacije.objects.filter(indexation_event=event).values_list(
            "original_invoice_id", flat=True
        )
    ) == {februar.pk, marec.pk}


def test_brez_prizadetega_izdanega_racuna_ni_poracuna(postavka):
    racun_s_postavkami(postavka, status=Invoice.Status.OSNUTEK)

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    assert not PoracunIndeksacije.objects.exists()


def test_izkljucen_dogodek_ne_postane_osnova(postavka):
    IndexationEvent.objects.create(
        lease_item=postavka,
        datum_uskladitve=date(LETO - 1, 2, 1),
        indeks_vrednost=Decimal("120.000"),
        stara_vrednost=Decimal("100.05"),
        nova_vrednost=Decimal("120.06"),
        izkljucen=True,
        razlog_izkljucitve="Razveljavljeno",
    )

    predlog = services.izracunaj_indeksacijo(postavka, Decimal("110.000"), DANES)

    assert predlog.nova_vrednost == Decimal("110.06")


def test_januar_in_tekoci_mesec_nista_zamujena(postavka):
    racun_s_postavkami(postavka, mesec=1)
    danes = date.today()  # noqa: DTZ011
    racun_s_postavkami(postavka, leto=danes.year, mesec=danes.month)

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    assert not PoracunIndeksacije.objects.exists()


def test_izpuscen_letnik_samodejno_dohiti(postavka):
    lease = postavka.lease
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    lease.prag_spremembe_odstotek = Decimal("2.00")
    lease.save(update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"])

    preskocen = services.izracunaj_indeksacijo(
        postavka, Decimal("101.000"), date(LETO, 1, 20)
    )
    assert not preskocen.pogoj_izpolnjen
    with pytest.raises(ValueError):
        services.potrdi_indeksacijo(postavka, Decimal("101.000"), date(LETO, 1, 20))
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()

    naslednji = services.izracunaj_indeksacijo(
        postavka, Decimal("102.500"), date(LETO + 1, 1, 20)
    )
    assert naslednji.pogoj_izpolnjen
    assert naslednji.odstotek_spremembe == Decimal("2.500")
    assert naslednji.nova_vrednost == Decimal("102.55")

    event = services.potrdi_indeksacijo(
        postavka, Decimal("102.500"), date(LETO + 1, 1, 20)
    )
    postavka.refresh_from_db()
    assert IndexationEvent.objects.count() == 1
    assert event.indeks_vrednost == Decimal("102.500")
    assert (event.stara_vrednost, event.nova_vrednost) == (
        Decimal("100.05"),
        Decimal("102.55"),
    )
    assert postavka.znesek_neto_mesecno == Decimal("102.55")


def test_razveljavitev_povrne_vrednost_in_zapise_audit(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is True
    assert event.razlog_izkljucitve
    assert IndexationEvent.objects.count() == 1

    audit = AuditLogEntry.objects.get()
    assert audit.content_type == ContentType.objects.get_for_model(LeaseItem)
    assert audit.object_id == postavka.pk
    assert audit.dejanje == AuditLogEntry.Dejanje.SPREMEMBA
    assert audit.polje == "znesek_neto_mesecno"
    assert (audit.stara_vrednost, audit.nova_vrednost) == ("110.06", "100.05")
    assert audit.razlog == f"Razveljavljena indeksacija #{event.pk}"


@pytest.mark.parametrize(
    "status", [Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN]
)
def test_razveljavitev_blokirana_po_izdanem_racunu(postavka, status):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    racun_s_postavkami(postavka, status=status)

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("110.06")
    assert event.izkljucen is False
    assert event.razlog_izkljucitve == ""
    assert not AuditLogEntry.objects.exists()


@pytest.mark.parametrize(
    "status", [Invoice.Status.NAPAKA, Invoice.Status.REZULTAT_NI_ZNAN]
)
def test_ostevilcen_racun_blokira_ne_glede_na_status(postavka, status):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    invoice = racun_s_postavkami(postavka, status=status)
    invoice.stevilka = "NP-00001"
    invoice.save(update_fields=["stevilka"])

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("110.06")
    assert event.izkljucen is False
    assert not AuditLogEntry.objects.exists()


def test_osnutek_brez_stevilke_ne_blokira_razveljavitve(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    racun_s_postavkami(postavka, status=Invoice.Status.OSNUTEK)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


def test_vrstica_druge_postavke_ne_blokira_razveljavitve(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    druga = LeaseItem.objects.create(
        lease=postavka.lease,
        naziv_postavke="Parkiranje",
        znesek_neto_mesecno=Decimal("25.00"),
        vat_regime=postavka.vat_regime,
        indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(LETO - 1, 1, 1),
    )
    racun_s_postavkami(druga)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert AuditLogEntry.objects.count() == 1


def test_razveljavitev_rollback_ob_napaki_audita(postavka, monkeypatch):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)

    def napaka(_self, *args, **kwargs):
        raise RuntimeError("napaka audita")

    monkeypatch.setattr(AuditLogEntry, "save", napaka)
    with pytest.raises(RuntimeError, match="napaka audita"):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("110.06")
    assert event.izkljucen is False
    assert event.razlog_izkljucitve == ""
    assert not AuditLogEntry.objects.exists()


def test_ze_izkljucen_event_se_ne_razveljavi_ponovno(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    services.razveljavi_indeksacijo(event)

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is True
    assert AuditLogEntry.objects.count() == 1


def test_racun_izdan_pred_uskladitvijo_ne_blokira(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES)
    racun_s_postavkami(postavka, mesec=1)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
