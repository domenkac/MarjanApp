from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from companies.models import Company, NumberSequence
from invoicing import services as invoicing_services
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from invoicing.services import (
    AktivniMesecniCikelObstaja,
    KonfliktPodatkovPrvegaKoraka,
    NeveljavenPrehodMesecnegaCikla,
    NiAktivnihPodjetij,
    ZakljucenoObdobje,
    premakni_mesecni_cikel,
    shrani_prvi_korak,
    zacni_ali_nadaljuj_mesecni_cikel,
)


def ustvari_podjetje(naziv, davcna_stevilka, *, aktivno=True):
    return Company.objects.create(
        drzava_koda="SI",
        naziv=naziv,
        davcna_stevilka=davcna_stevilka,
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja=f"marjanapp-{davcna_stevilka}",
        aktivno=aktivno,
    )


@pytest.mark.django_db
def test_model_zavrne_nenormalizirano_obdobje():
    cycle = MonthlyCycle(
        obdobje=date(2026, 8, 19),
        datum_izdaje=date(2026, 8, 12),
        status=MonthlyCycle.Status.ZAKLJUCEN,
    )

    with pytest.raises(ValidationError, match="prvi dan"):
        cycle.full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize("korak", [0, 7])
def test_db_zavrne_neveljaven_korak(korak):
    with pytest.raises(IntegrityError), transaction.atomic():
        MonthlyCycle.objects.create(
            obdobje=date(2026, korak + 1, 1),
            datum_izdaje=date(2026, 1, 1),
            trenutni_korak=korak,
        )


@pytest.mark.django_db
def test_db_zavrne_podvojeno_obdobje_in_drugi_aktivni_cikel():
    MonthlyCycle.objects.create(
        obdobje=date(2026, 1, 1), datum_izdaje=date(2026, 1, 1)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MonthlyCycle.objects.create(
            obdobje=date(2026, 1, 1),
            datum_izdaje=date(2026, 1, 2),
            status=MonthlyCycle.Status.ZAKLJUCEN,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        MonthlyCycle.objects.create(
            obdobje=date(2026, 2, 1), datum_izdaje=date(2026, 2, 1)
        )


@pytest.mark.django_db
def test_db_zavrne_podvojeno_clanstvo_podjetja():
    company = ustvari_podjetje("Alfa d.o.o.", "15012557")
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 1, 1), datum_izdaje=date(2026, 1, 1)
    )
    MonthlyBatch.objects.create(cycle=cycle, company=company)

    with pytest.raises(IntegrityError), transaction.atomic():
        MonthlyBatch.objects.create(cycle=cycle, company=company)


@pytest.mark.django_db
def test_prvi_zagon_posname_aktivna_podjetja_samo_enkrat():
    aktivno = ustvari_podjetje("Alfa d.o.o.", "15012557")
    neaktivno = ustvari_podjetje("Beta d.o.o.", "69165028", aktivno=False)

    cycle = zacni_ali_nadaljuj_mesecni_cikel(
        date(2026, 8, 18), date(2026, 8, 12)
    )
    isti = zacni_ali_nadaljuj_mesecni_cikel(
        date(2026, 8, 1), date(2026, 8, 12)
    )
    aktivno.aktivno = False
    aktivno.save(update_fields=["aktivno"])
    neaktivno.aktivno = True
    neaktivno.save(update_fields=["aktivno"])

    assert isti.pk == cycle.pk
    assert list(cycle.batches.values_list("company_id", flat=True)) == [aktivno.pk]
    assert MonthlyCycle.objects.count() == 1
    assert MonthlyBatch.objects.count() == 1


@pytest.mark.django_db
def test_drugi_mesec_ni_tiho_ustvarjen_in_zakljuceno_obdobje_se_ne_ponovi():
    ustvari_podjetje("Alfa d.o.o.", "15012557")
    zacni_ali_nadaljuj_mesecni_cikel(date(2026, 8, 1), date(2026, 8, 1))
    with pytest.raises(AktivniMesecniCikelObstaja):
        zacni_ali_nadaljuj_mesecni_cikel(date(2026, 9, 1), date(2026, 9, 1))

    MonthlyCycle.objects.update(status=MonthlyCycle.Status.ZAKLJUCEN)
    with pytest.raises(ZakljucenoObdobje):
        zacni_ali_nadaljuj_mesecni_cikel(date(2026, 8, 1), date(2026, 8, 1))
    assert MonthlyCycle.objects.count() == 1


@pytest.mark.django_db
def test_shranjevanje_prvega_koraka_ne_ustvari_racuna_ali_zaporedja():
    ustvari_podjetje("Alfa d.o.o.", "15012557")

    cycle = shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 12))

    assert cycle.trenutni_korak == 2
    assert cycle.datum_izdaje == date(2026, 8, 12)
    assert Invoice.objects.count() == 0
    assert NumberSequence.objects.count() == 0


@pytest.mark.django_db
def test_identicen_retry_je_idempotenten_konflikten_datum_pa_zavrnjen():
    ustvari_podjetje("Alfa d.o.o.", "15012557")
    cycle = shrani_prvi_korak(date(2026, 8, 18), date(2026, 8, 12))

    isti = shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 12))
    with pytest.raises(KonfliktPodatkovPrvegaKoraka):
        shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 13))

    cycle.refresh_from_db()
    assert isti.pk == cycle.pk
    assert cycle.datum_izdaje == date(2026, 8, 12)
    assert cycle.trenutni_korak == 2


@pytest.mark.django_db
def test_zacetek_primerja_datum_ob_navadnem_nadaljevanju():
    ustvari_podjetje("Alfa d.o.o.", "15012557")
    cycle = zacni_ali_nadaljuj_mesecni_cikel(
        date(2026, 8, 1), date(2026, 8, 12)
    )

    isti = zacni_ali_nadaljuj_mesecni_cikel(
        date(2026, 8, 1), date(2026, 8, 12)
    )
    with pytest.raises(KonfliktPodatkovPrvegaKoraka):
        zacni_ali_nadaljuj_mesecni_cikel(
            date(2026, 8, 1), date(2026, 8, 13)
        )

    cycle.refresh_from_db()
    assert isti.pk == cycle.pk
    assert cycle.datum_izdaje == date(2026, 8, 12)


@pytest.mark.django_db
def test_integrityerror_obnova_primerja_podatke_zmagovalca(monkeypatch):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=2,
    )
    monkeypatch.setattr(
        invoicing_services,
        "_zacni_ali_nadaljuj_v_transakciji",
        lambda *args: (_ for _ in ()).throw(IntegrityError("race")),
    )

    isti = shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 12))
    with pytest.raises(KonfliktPodatkovPrvegaKoraka):
        shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 13))

    assert isti.pk == cycle.pk


@pytest.mark.django_db
def test_prvi_korak_brez_aktivnih_podjetij_ne_ustvari_cikla():
    with pytest.raises(NiAktivnihPodjetij):
        shrani_prvi_korak(date(2026, 8, 1), date(2026, 8, 12))

    assert MonthlyCycle.objects.count() == 0
    assert MonthlyBatch.objects.count() == 0


@pytest.mark.django_db
def test_premik_dovoli_samo_naslednji_korak_in_idempotenten_retry():
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=2,
    )

    assert premakni_mesecni_cikel(cycle, 2).trenutni_korak == 2
    assert premakni_mesecni_cikel(cycle, 3).trenutni_korak == 3


@pytest.mark.django_db
@pytest.mark.parametrize("cilj", [1, 4])
def test_premik_zavrne_nazaj_in_preskok(cilj):
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=2,
    )

    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        premakni_mesecni_cikel(cycle, cilj)

    cycle.refresh_from_db()
    assert cycle.trenutni_korak == 2


@pytest.mark.django_db
def test_zastarel_klicatelj_ne_prepisuje_novejsega_stanja():
    zastarel = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=2,
    )
    MonthlyCycle.objects.filter(pk=zastarel.pk).update(trenutni_korak=3)

    svezi = premakni_mesecni_cikel(zastarel, 3)

    assert svezi.trenutni_korak == 3
    assert MonthlyCycle.objects.get(pk=zastarel.pk).trenutni_korak == 3


@pytest.mark.django_db
def test_zakljucenega_cikla_ni_mogoce_premakniti():
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=2,
        status=MonthlyCycle.Status.ZAKLJUCEN,
    )

    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        premakni_mesecni_cikel(cycle, 3)

    cycle.refresh_from_db()
    assert cycle.trenutni_korak == 2
