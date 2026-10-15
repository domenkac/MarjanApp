from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

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
from core.services import record_edit_fingerprint
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import potrdi_korekcijski_osnutek, pripravi_korekcijski_osnutek
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.image_data import slika_bytes
from tests.setup_helpers import prepare_initial_setup

LETO = 2026
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
        zahteva_polni_odbitek=False,
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
    save_financial_fixture(lease, update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"])

    predlog = services.izracunaj_indeksacijo(postavka, Decimal(nov_indeks), DANES, indeks_leto=LETO - 1)

    assert predlog.pogoj_izpolnjen is izpolnjen
    assert predlog.razlog
    if razlog:
        assert razlog in predlog.razlog
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()


def test_izracun_zaokrozi_half_up_in_verizno_primerja(postavka):
    predlog = services.izracunaj_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    assert predlog.stara_vrednost == Decimal("100.05")
    assert predlog.nova_vrednost == Decimal("110.06")
    assert predlog.odstotek_spremembe == Decimal("10.0")

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    postavka.refresh_from_db()
    naslednji = services.izracunaj_indeksacijo(postavka, Decimal("121.000"), DANES, indeks_leto=LETO)
    assert naslednji.stara_vrednost == Decimal("110.06")
    assert naslednji.nova_vrednost == Decimal("121.07")
    assert naslednji.odstotek_spremembe == Decimal("10.0")


def test_potrditev_shrani_zgodovino_ne_spremeni_reference(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    postavka.refresh_from_db()
    event.refresh_from_db()

    assert event.lease_item == postavka
    assert event.datum_uskladitve == date(LETO, 2, 1)
    assert event.indeks_vrednost == Decimal("110.000")
    assert (event.stara_vrednost, event.nova_vrednost) == (
        Decimal("100.05"),
        Decimal("110.06"),
    )
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


def test_neizpolnjen_pogoj_ne_ustvari_dogodka(postavka):
    postavka.lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    postavka.lease.prag_spremembe_odstotek = Decimal("2.00")
    save_financial_fixture(postavka.lease,
        update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"]
    )

    with pytest.raises(ValueError):
        services.potrdi_indeksacijo(postavka, Decimal("101.000"), DANES, indeks_leto=LETO - 1)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()


def test_potrditev_ob_napaki_razveljavi_dogodek_in_znesek(postavka, monkeypatch):
    def napaka(_event, **_kwargs):
        raise RuntimeError("napaka zaznave")

    monkeypatch.setattr(services, "zaznaj_zamujen_poracun", napaka)
    with pytest.raises(RuntimeError, match="napaka zaznave"):
        services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)

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
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)

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
    assert services.zaznaj_zamujen_poracun(event, obdobje_zaznave=DANES) == []


def test_storno_vrstica_ne_podvoji_poracuna_istega_meseca(postavka, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(slika_bytes())
    (tmp_path / "podpis.png").write_bytes(slika_bytes())
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
    postavka.vat_regime.eslog_koda = "S"
    save_financial_fixture(postavka.vat_regime, update_fields=["eslog_koda"])
    prepare_initial_setup(original.company, postavka.lease)
    potrdi_korekcijski_osnutek(storno)

    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(LETO, 4, 15), indeks_leto=LETO - 1)
    poracuni = list(PoracunIndeksacije.objects.filter(indexation_event=event))
    assert len(poracuni) == 1
    assert poracuni[0].original_invoice_id == original.pk


def test_zamuda_vec_mesecev_ustvari_locena_poracuna(postavka):
    februar = racun_s_postavkami(postavka, mesec=2)
    marec = racun_s_postavkami(postavka, mesec=3)
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(LETO, 4, 15), indeks_leto=LETO - 1)

    assert set(
        PoracunIndeksacije.objects.filter(indexation_event=event).values_list(
            "original_invoice_id", flat=True
        )
    ) == {februar.pk, marec.pk}


def test_brez_prizadetega_izdanega_racuna_ni_poracuna(postavka):
    racun_s_postavkami(postavka, status=Invoice.Status.OSNUTEK)

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)

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

    predlog = services.izracunaj_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)

    assert predlog.nova_vrednost == Decimal("110.06")


def test_januar_ni_prizadet_izdani_tekoci_mesec_je_vkljucen(postavka):
    racun_s_postavkami(postavka, mesec=1)
    danes = DANES
    current = racun_s_postavkami(postavka, leto=danes.year, mesec=danes.month)

    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)

    assert PoracunIndeksacije.objects.get().original_invoice == current


def test_izpuscen_letnik_samodejno_dohiti(postavka):
    lease = postavka.lease
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    lease.prag_spremembe_odstotek = Decimal("2.00")
    save_financial_fixture(lease, update_fields=["tip_pogoja_indeksacije", "prag_spremembe_odstotek"])

    preskocen = services.izracunaj_indeksacijo(
        postavka, Decimal("101.000"), date(LETO, 1, 20), indeks_leto=LETO - 1
    )
    assert not preskocen.pogoj_izpolnjen
    with pytest.raises(ValueError):
        services.potrdi_indeksacijo(postavka, Decimal("101.000"), date(LETO, 1, 20), indeks_leto=LETO - 1)
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert not IndexationEvent.objects.exists()

    naslednji = services.izracunaj_indeksacijo(
        postavka, Decimal("102.500"), date(LETO + 1, 1, 20), indeks_leto=LETO
    )
    assert naslednji.pogoj_izpolnjen
    assert naslednji.odstotek_spremembe == Decimal("2.500")
    assert naslednji.nova_vrednost == Decimal("102.55")

    event = services.potrdi_indeksacijo(
        postavka, Decimal("102.500"), date(LETO + 1, 1, 20), indeks_leto=LETO
    )
    postavka.refresh_from_db()
    assert IndexationEvent.objects.count() == 1
    assert event.indeks_vrednost == Decimal("102.500")
    assert (event.stara_vrednost, event.nova_vrednost) == (
        Decimal("100.05"),
        Decimal("102.55"),
    )
    assert services.najemnina_na_datum(postavka, date(LETO + 1, 2, 1)) == Decimal("102.55")


@pytest.mark.parametrize("indeks", [Decimal("110.000"), Decimal("100.000")])
def test_razveljavitev_auditira_dogodek_brez_izmisljene_spremembe_najemnine(postavka, indeks):
    event = services.potrdi_indeksacijo(postavka, indeks, DANES, indeks_leto=LETO - 1)
    event.razlog_izkljucitve = "Prvotna opomba"
    event.save(update_fields=["razlog_izkljucitve"])
    history = list(AuditLogEntry.objects.values())

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is True
    assert event.razlog_izkljucitve
    assert IndexationEvent.objects.count() == 1

    assert not AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).exists()
    audits = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(IndexationEvent),
        object_id=event.pk,
    ).filter(polje__in=["izkljucen", "razlog_izkljucitve"])
    razlog = f"Razveljavljena indeksacija #{event.pk}"
    assert {audit.polje: (audit.stara_vrednost, audit.nova_vrednost) for audit in audits} == {
        "izkljucen": ("False", "True"),
        "razlog_izkljucitve": ("Prvotna opomba", razlog),
    }
    assert all(audit.dejanje == AuditLogEntry.Dejanje.SPREMEMBA and audit.razlog == razlog for audit in audits)
    assert list(AuditLogEntry.objects.filter(pk__in=[row["id"] for row in history]).values()) == history


def test_razveljavitev_legacy_predpomnjene_najemnine_zapise_dejanski_spremembi(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk), znesek_neto_mesecno=event.nova_vrednost)
    services.razveljavi_indeksacijo(event)
    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is True
    audit = AuditLogEntry.objects.get(content_type=ContentType.objects.get_for_model(LeaseItem))
    assert audit.object_id == postavka.pk
    assert audit.polje == "znesek_neto_mesecno"
    assert (audit.stara_vrednost, audit.nova_vrednost) == ("110.06", "100.05")
    event_audits = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(IndexationEvent), object_id=event.pk,
    ).filter(polje__in=["izkljucen", "razlog_izkljucitve"])
    assert event_audits.count() == 2
    assert all(row.razlog == event.razlog_izkljucitve for row in [audit, *event_audits])


@pytest.mark.parametrize(
    "status", [Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN]
)
def test_razveljavitev_blokirana_po_izdanem_racunu(postavka, status):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    racun_s_postavkami(postavka, status=status)

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is False
    assert event.razlog_izkljucitve == ""
    assert AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).count() == 0


@pytest.mark.parametrize(
    "status", [Invoice.Status.NAPAKA, Invoice.Status.REZULTAT_NI_ZNAN]
)
def test_ostevilcen_racun_blokira_ne_glede_na_status(postavka, status):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    invoice = racun_s_postavkami(postavka, status=status)
    invoice.stevilka = "NP-00001"
    invoice.save(update_fields=["stevilka"])

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is False
    assert AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).count() == 0


def test_osnutek_brez_stevilke_ne_blokira_razveljavitve(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    racun_s_postavkami(postavka, status=Invoice.Status.OSNUTEK)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


def test_vrstica_druge_postavke_ne_blokira_razveljavitve(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
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
    assert AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).count() == 0


@pytest.mark.parametrize(("legacy", "fail_on"), [(False, 1), (False, 2), (True, 1), (True, 2), (True, 3)])
def test_razveljavitev_rollback_ob_napaki_audita(postavka, monkeypatch, legacy, fail_on):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    if legacy:
        update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk), znesek_neto_mesecno=event.nova_vrednost)
    persisted_rent = LeaseItem.objects.get(pk=postavka.pk).znesek_neto_mesecno
    history = list(AuditLogEntry.objects.values())
    original_save = AuditLogEntry.save
    calls = 0

    def napaka(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise RuntimeError("napaka audita")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(AuditLogEntry, "save", napaka)
    with pytest.raises(RuntimeError, match="napaka audita"):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == persisted_rent
    assert event.izkljucen is False
    assert event.razlog_izkljucitve == ""
    assert AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).count() == 0
    assert list(AuditLogEntry.objects.values()) == history


def test_ze_izkljucen_event_se_ne_razveljavi_ponovno(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    services.razveljavi_indeksacijo(event)

    with pytest.raises(ValueError):
        services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    event.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")
    assert event.izkljucen is True
    assert AuditLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(LeaseItem)).count() == 0


def test_racun_izdan_pred_uskladitvijo_ne_blokira(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=LETO - 1)
    racun_s_postavkami(postavka, mesec=1)

    services.razveljavi_indeksacijo(event)

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


@pytest.mark.parametrize("potrjeno", [date(2026, 1, 20), date(2026, 3, 15), date(2027, 3, 15)])
def test_ucinkovanje_neodvisno_od_potrditve(postavka, potrjeno):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), potrjeno, indeks_leto=2025)
    assert event.datum_uskladitve == date(2026, 2, 1)
    assert services.izracunaj_postavko(postavka, date(2026, 1, 1), date(2026, 1, 31)).osnova == Decimal("100.05")
    assert services.izracunaj_postavko(postavka, date(2026, 2, 1), date(2026, 2, 28)).osnova == Decimal("110.06")
    assert services.najemnina_na_datum(postavka, date(2025, 6, 1)) == Decimal("100.05")


def test_vec_let_in_zgodovinski_predlog(postavka):
    services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    services.potrdi_indeksacijo(postavka, Decimal("121.000"), date(2027, 1, 20), indeks_leto=2026)
    assert services.najemnina_na_datum(postavka, date(2027, 1, 31)) == Decimal("110.06")
    assert services.najemnina_na_datum(postavka, date(2027, 2, 1)) == Decimal("121.07")
    predlog = services.izracunaj_indeksacijo(postavka, Decimal("110.000"), date(2028, 1, 20), indeks_leto=2025)
    assert predlog.stara_vrednost == Decimal("100.05")
    assert predlog.nova_vrednost == Decimal("110.06")


def test_prag_enakost_in_padec(postavka):
    lease = postavka.lease
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    lease.prag_spremembe_odstotek = Decimal("2.00")
    save_financial_fixture(lease)
    for indeks in (Decimal("102.000"), Decimal("98.000")):
        assert services.izracunaj_indeksacijo(postavka, indeks, DANES, indeks_leto=2025).pogoj_izpolnjen
    lease.ima_klavzulo_indeksacije = False
    save_financial_fixture(lease)
    assert not services.izracunaj_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025).pogoj_izpolnjen


@pytest.mark.parametrize("indeks", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_neveljaven_indeks_brez_sprememb(postavka, indeks):
    with pytest.raises(ValidationError):
        services.potrdi_indeksacijo(postavka, indeks, DANES, indeks_leto=2025)
    assert not IndexationEvent.objects.exists()


def test_podvojena_potrditev_in_db_omejitev(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    with pytest.raises(ValidationError, match="Podvojena"):
        services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    with pytest.raises(IntegrityError), transaction.atomic():
        IndexationEvent.objects.create(
            lease_item=postavka, datum_uskladitve=event.datum_uskladitve,
            indeks_vrednost=event.indeks_vrednost, stara_vrednost=event.stara_vrednost,
            nova_vrednost=event.nova_vrednost,
        )
    assert IndexationEvent.objects.count() == 1
    assert AuditLogEntry.objects.count() == 1


def test_nevarna_razveljavitev_odvisnega_dogodka(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    services.potrdi_indeksacijo(postavka, Decimal("121.000"), date(2027, 1, 20), indeks_leto=2026)
    with pytest.raises(ValidationError, match="odvisni"):
        services.razveljavi_indeksacijo(event)
    assert services.najemnina_na_datum(postavka, date(2027, 2, 1)) == Decimal("121.07")


def test_razveljavitev_po_obdobju_ne_datumu_izdaje(postavka):
    event = services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(2026, 1, 20), indeks_leto=2025)
    invoice = racun_s_postavkami(postavka)
    invoice.datum_izdaje = date(2026, 1, 25)
    invoice.save()
    with pytest.raises(ValueError, match="izdan račun"):
        services.razveljavi_indeksacijo(event)


def test_sorazmerni_prestopni_februar(postavka):
    services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(2028, 1, 20), indeks_leto=2027)
    postavka.datum_prenehanja_postavke = date(2028, 2, 15)
    save_financial_fixture(postavka)
    izracun = services.izracunaj_postavko(postavka, date(2028, 2, 1), date(2028, 2, 29))
    assert izracun.osnova == Decimal("56.93")
    assert izracun.ddv_znesek == Decimal("12.52")
    assert izracun.je_sorazmerno


def test_legacy_reference_in_rocni_popravki_blokirani(postavka):
    update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk), referencna_najemnina=None, datum_referencne_najemnine=None)
    postavka.refresh_from_db()
    postavka.save()
    assert postavka.referencna_najemnina is None
    with pytest.raises(ValidationError, match="nerazjasnjena"):
        services.najemnina_na_datum(postavka, date(2026, 1, 1))
    with pytest.raises(ValidationError, match="datumu učinkovanja"):
        services.rocno_popravi_postavko(postavka, source_fingerprint=record_edit_fingerprint(postavka), razlog="Popravek", znesek_neto_mesecno=Decimal("200.00"))
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


def test_neznan_rocni_znesek_in_obdobje_pred_referenco(postavka):
    with pytest.raises(ValidationError, match="pred preverjeno"):
        services.najemnina_na_datum(postavka, date(2024, 1, 1))
    postavka.znesek_neto_mesecno = Decimal("200.00")
    save_financial_fixture(postavka)
    with pytest.raises(ValidationError, match="nepojasnjen ročni"):
        services.najemnina_na_datum(postavka, date(2026, 1, 1))


def test_zgodovinski_rocni_audit_blokira(postavka):
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(LeaseItem), object_id=postavka.pk,
        polje="znesek_neto_mesecno", stara_vrednost="90.00", nova_vrednost="100.05", razlog="Ročni popravek",
    )
    with pytest.raises(ValidationError, match="ročni popravki"):
        services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    assert not IndexationEvent.objects.exists()


def test_nevarna_vstavitev_pred_kasnejsi_dogodek(postavka):
    services.potrdi_indeksacijo(postavka, Decimal("121.000"), date(2027, 1, 20), indeks_leto=2026)
    with pytest.raises(ValidationError, match="odvisni"):
        services.potrdi_indeksacijo(postavka, Decimal("110.000"), DANES, indeks_leto=2025)
    assert IndexationEvent.objects.count() == 1


def test_zgodovinski_mesec_ne_validira_prihodnjega_dogodka(postavka):
    IndexationEvent.objects.create(
        lease_item=postavka, datum_uskladitve=date(2028, 2, 1),
        indeks_vrednost=Decimal("110.000"), stara_vrednost=Decimal("999.00"),
        nova_vrednost=Decimal("110.06"),
    )
    assert services.izracunaj_postavko(postavka, date(2026, 1, 1), date(2026, 1, 31)).osnova == Decimal("100.05")
    with pytest.raises(ValidationError, match="veriga"):
        services.najemnina_na_datum(postavka, date(2028, 2, 1))


def test_obdobje_cez_ucinkovanje_zahteva_locen_obracun(postavka):
    services.potrdi_indeksacijo(postavka, Decimal("110.000"), date(2026, 1, 20), indeks_leto=2025)
    with pytest.raises(ValidationError, match="prečka"):
        services.izracunaj_postavko(postavka, date(2026, 1, 15), date(2026, 2, 15))


def test_referenca_ob_preklopu_ne_dokazuje_najemnine_ob_sklenitvi(postavka):
    item = LeaseItem.objects.create(
        lease=postavka.lease, vat_regime=postavka.vat_regime,
        znesek_neto_mesecno=Decimal("150.00"), indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(2026, 1, 1),
    )
    assert item.datum_referencne_najemnine == date(2026, 1, 1)
    with pytest.raises(ValidationError, match="pred preverjeno"):
        services.najemnina_na_datum(item, date(2025, 6, 1))
    assert services.najemnina_na_datum(item, date(2026, 1, 1)) == Decimal("150.00")


def test_samo_navzgor_zelo_majhen_indeks_ostane_neizpolnjen(postavka):
    postavka.lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.SAMO_NAVZGOR
    save_financial_fixture(postavka.lease)
    predlog = services.izracunaj_indeksacijo(postavka, Decimal("0.001"), DANES, indeks_leto=2025)
    assert not predlog.pogoj_izpolnjen
