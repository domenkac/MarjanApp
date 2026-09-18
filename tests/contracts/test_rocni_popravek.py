from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from contracts.models import LeaseItem
from contracts.services import rocno_popravi_postavko
from core.models import AuditLogEntry
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def revizije(postavka):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(LeaseItem),
        object_id=postavka.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
    ).order_by("pk")


@pytest.mark.parametrize("razlog", [None, "", "   "])
def test_rocni_popravek_zahteva_razlog(pogodba, razlog):
    postavka = dodaj_postavko(pogodba)
    with pytest.raises(ValidationError) as error:
        rocno_popravi_postavko(
            postavka, razlog=razlog, znesek_neto_mesecno=Decimal("120.00")
        )
    assert "razlog" in error.value.message_dict
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.00")
    assert not revizije(postavka).exists()


def test_sprememba_zneska_uporabi_sveze_stanje_in_zapise_audit(pogodba):
    postavka = dodaj_postavko(pogodba)
    postavka.znesek_neto_mesecno = Decimal("999.00")

    popravljena = rocno_popravi_postavko(
        postavka, razlog="Dogovorjena najemnina", znesek_neto_mesecno=Decimal("120.0")
    )

    assert popravljena.znesek_neto_mesecno == Decimal("120.00")
    revizija = revizije(postavka).get()
    assert (revizija.content_type.model_class(), revizija.object_id) == (
        LeaseItem, postavka.pk
    )
    assert (
        revizija.dejanje, revizija.polje, revizija.stara_vrednost,
        revizija.nova_vrednost, revizija.razlog,
    ) == (
        AuditLogEntry.Dejanje.SPREMEMBA,
        "znesek_neto_mesecno", "100.00", "120.00", "Dogovorjena najemnina"
    )


@pytest.mark.parametrize(
    ("polje", "nova"),
    [
        ("datum_zacetka_postavke", date(2026, 2, 1)),
        ("datum_prenehanja_postavke", date(2026, 12, 31)),
    ],
)
def test_sprememba_datuma_zapise_audit(pogodba, polje, nova):
    postavka = dodaj_postavko(pogodba)
    rocno_popravi_postavko(postavka, razlog="Aneks", **{polje: nova})
    postavka.refresh_from_db()

    assert getattr(postavka, polje) == nova
    revizija = revizije(postavka).get()
    assert (revizija.polje, revizija.stara_vrednost, revizija.nova_vrednost) == (
        polje, "None", str(nova)
    )
    assert revizija.razlog == "Aneks"


def test_vec_sprememb_ustvari_locene_audite(pogodba):
    postavka = dodaj_postavko(pogodba)
    rocno_popravi_postavko(
        postavka,
        razlog="Aneks 2",
        znesek_neto_mesecno=Decimal("125.00"),
        datum_zacetka_postavke=date(2026, 2, 1),
        datum_prenehanja_postavke=date(2026, 12, 31),
    )

    assert {
        r.polje: (r.stara_vrednost, r.nova_vrednost, r.razlog)
        for r in revizije(postavka)
    } == {
        "znesek_neto_mesecno": ("100.00", "125.00", "Aneks 2"),
        "datum_zacetka_postavke": ("None", "2026-02-01", "Aneks 2"),
        "datum_prenehanja_postavke": ("None", "2026-12-31", "Aneks 2"),
    }


def test_ista_vrednost_ne_ustvari_audita(pogodba):
    postavka = dodaj_postavko(pogodba)
    rocno_popravi_postavko(
        postavka,
        razlog="Preverjeno",
        znesek_neto_mesecno=Decimal("100.0"),
        datum_zacetka_postavke=None,
    )
    assert not revizije(postavka).exists()


def test_nedovoljeno_polje_je_zavrnjeno(pogodba):
    postavka = dodaj_postavko(pogodba)
    with pytest.raises(ValueError, match="Nedovoljena"):
        rocno_popravi_postavko(
            postavka, razlog="Poskus", naziv_postavke="Nov opis"
        )
    postavka.refresh_from_db()
    assert postavka.naziv_postavke == "Najemnina"
    assert not revizije(postavka).exists()


def test_neveljavna_nova_vrednost_rollbacka_vse(pogodba):
    postavka = dodaj_postavko(pogodba)
    with pytest.raises(ValidationError):
        rocno_popravi_postavko(
            postavka, razlog="Napačen vnos",
            znesek_neto_mesecno=Decimal("0.00"),
            datum_zacetka_postavke=date(2026, 2, 1),
        )
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.00")
    assert postavka.datum_zacetka_postavke is None
    assert not revizije(postavka).exists()


def test_db_napaka_pri_drugem_auditu_rollbacka_postavko_in_prvega(pogodba, monkeypatch):
    postavka = dodaj_postavko(pogodba)
    shrani = AuditLogEntry.save

    def napaka_pri_drugem(self, *args, **kwargs):
        if self.polje == "datum_zacetka_postavke":
            raise IntegrityError("napaka pri auditu")
        return shrani(self, *args, **kwargs)

    monkeypatch.setattr(AuditLogEntry, "save", napaka_pri_drugem)
    with pytest.raises(IntegrityError, match="napaka pri auditu"):
        rocno_popravi_postavko(
            postavka, razlog="Aneks",
            znesek_neto_mesecno=Decimal("120.00"),
            datum_zacetka_postavke=date(2026, 2, 1),
        )

    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.00")
    assert postavka.datum_zacetka_postavke is None
    assert not revizije(postavka).exists()


def test_neposreden_orm_save_ni_rocni_domain_service(pogodba):
    postavka = dodaj_postavko(pogodba)
    postavka.znesek_neto_mesecno = Decimal("120.00")
    postavka.save(update_fields=["znesek_neto_mesecno"])
    assert not revizije(postavka).exists()