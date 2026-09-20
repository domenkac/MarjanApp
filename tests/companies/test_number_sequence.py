from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from django.db import IntegrityError, connections, transaction

from companies.models import Company, NumberSequence
from companies.services import dodeli_naslednjo_stevilko


def ustvari_podjetje():
    return Company.objects.create(
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


@pytest.mark.django_db
def test_np_ne_resetira_cez_leto():
    company = ustvari_podjetje()

    prva = dodeli_naslednjo_stevilko(company, NumberSequence.Tip.NP, date(2025, 12, 31))
    druga = dodeli_naslednjo_stevilko(company, NumberSequence.Tip.NP, date(2026, 1, 1))

    assert (prva, druga) == (1, 2)
    assert NumberSequence.objects.get().leto == 0


@pytest.mark.django_db
def test_st_resetira_po_letu_izdaje():
    company = ustvari_podjetje()

    prva = dodeli_naslednjo_stevilko(company, NumberSequence.Tip.ST, date(2025, 12, 31))
    druga = dodeli_naslednjo_stevilko(company, NumberSequence.Tip.ST, date(2026, 1, 1))

    assert (prva, druga) == (1, 1)
    assert set(NumberSequence.objects.values_list("leto", flat=True)) == {2025, 2026}


@pytest.mark.django_db
def test_np_uporablja_leto_nic_in_je_enolicen():
    company = ustvari_podjetje()
    NumberSequence.objects.create(company=company, tip=NumberSequence.Tip.NP, leto=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        NumberSequence.objects.create(
            company=company, tip=NumberSequence.Tip.NP, leto=0
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tip", "leto"),
    [("NP", 2026), ("ST", 0)],
)
def test_number_sequence_zavrne_neveljavno_kombinacijo_tipa_in_leta(tip, leto):
    company = ustvari_podjetje()

    with pytest.raises(IntegrityError), transaction.atomic():
        NumberSequence.objects.create(company=company, tip=tip, leto=leto)


@pytest.mark.django_db
def test_neuspesna_transakcija_ne_zazge_stevilke():
    company = ustvari_podjetje()

    with pytest.raises(RuntimeError), transaction.atomic():
        dodeli_naslednjo_stevilko(company, NumberSequence.Tip.NP, date(2026, 1, 1))
        raise RuntimeError

    assert (
        dodeli_naslednjo_stevilko(company, NumberSequence.Tip.NP, date(2026, 1, 1)) == 1
    )


@pytest.mark.django_db(transaction=True)
def test_atomarno_dodeljevanje_socasno():
    company = ustvari_podjetje()
    zacetek = Barrier(2)

    def dodeli():
        povezava = connections["default"]
        povezava.close()
        povezava.connect()
        db_povezava = povezava.connection
        zacetek.wait()
        try:
            stevilka = dodeli_naslednjo_stevilko(
                company, NumberSequence.Tip.NP, date(2026, 1, 1)
            )
            return stevilka, id(db_povezava)
        finally:
            povezava.close()

    with ThreadPoolExecutor(max_workers=2) as izvajalec:
        rezultata = list(izvajalec.map(lambda _: dodeli(), range(2)))

    assert sorted(stevilka for stevilka, _ in rezultata) == [1, 2]
    assert len({povezava for _, povezava in rezultata}) == 2
    assert NumberSequence.objects.get().zadnja_stevilka == 2
