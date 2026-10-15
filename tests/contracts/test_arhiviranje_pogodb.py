from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType

from companies.services import arhiviraj_poslovni_zapis as arhiviraj_podjetje
from contracts.models import LeaseItem
from contracts.services import (
    arhiviraj_poslovni_zapis,
    ponovno_aktiviraj_poslovni_zapis,
)
from core.models import AuditLogEntry
from invoicing.models import InvoiceLine
from invoicing.services import potrdi_osnutek_np, pripravi_osnutek_np
from tests.financial_fixtures import update_financial_fixture
from tests.invoicing.test_priprava_np import dodaj_postavko
from tests.setup_helpers import prepare_initial_setup

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def cleni(pogodba):
    item = dodaj_postavko(pogodba)
    unit = pogodba.unit
    property = unit.property
    return {
        "Company": property.company,
        "Property": property,
        "Unit": unit,
        "Tenant": pogodba.tenant,
        "Lease": pogodba,
        "LeaseItem": item,
    }


POLJA = {
    "Company": ("aktivno", False),
    "Property": ("aktivna", False),
    "Unit": ("aktivna", False),
    "Tenant": ("aktiven", False),
    "Lease": ("arhivirana", True),
    "LeaseItem": ("arhivirana", True),
}


@pytest.mark.parametrize("tip", [tip for tip in POLJA if tip != "Company"])
def test_arhiviranje_ohrani_zgodovino_in_izkljuci_novi_np(pogodba, tip):
    veriga = cleni(pogodba)
    invoice = pripravi_osnutek_np(
        pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    prepare_initial_setup(invoice.company, invoice.lease)
    invoice = potrdi_osnutek_np(invoice)
    line = InvoiceLine.objects.get(invoice=invoice)
    zapis = veriga[tip]

    rezultat = arhiviraj_poslovni_zapis(zapis)

    polje, arhivirana_vrednost = POLJA[tip]
    zapis.refresh_from_db()
    assert rezultat.uspesno is True
    assert getattr(zapis, polje) is arhivirana_vrednost
    assert InvoiceLine.objects.get(pk=line.pk).lease_item_id == veriga["LeaseItem"].pk
    invoice.refresh_from_db()
    assert (invoice.stevilka, invoice.skupaj_z_ddv) == ("NP-00001", Decimal("122.00"))
    assert invoice.company_id == veriga["Company"].pk
    assert invoice.lease_id == veriga["Lease"].pk
    for ime, otrok in veriga.items():
        if ime != tip:
            otrok.refresh_from_db()
            assert getattr(otrok, POLJA[ime][0]) is not POLJA[ime][1]
    assert (
        pripravi_osnutek_np(
            pogodba, date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)
        ).invoice
        is None
    )
    audit = AuditLogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(type(zapis)),
        object_id=zapis.pk,
    )
    assert (audit.dejanje, audit.polje, audit.nova_vrednost) == (
        AuditLogEntry.Dejanje.SPREMEMBA,
        polje,
        str(arhivirana_vrednost),
    )


@pytest.mark.parametrize("tip", [tip for tip in POLJA if tip != "Company"])
def test_veljavna_ponovna_aktivacija_uspe(pogodba, tip):
    veriga = cleni(pogodba)
    zapis = veriga[tip]

    assert arhiviraj_poslovni_zapis(zapis).uspesno is True
    rezultat = ponovno_aktiviraj_poslovni_zapis(zapis)

    polje, arhivirana_vrednost = POLJA[tip]
    zapis.refresh_from_db()
    assert (rezultat.uspesno, rezultat.razlogi) == (True, ())
    assert getattr(zapis, polje) is not arhivirana_vrednost
    revizije = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(type(zapis)),
        object_id=zapis.pk,
    ).order_by("pk")
    assert list(revizije.values_list("stara_vrednost", "nova_vrednost")) == [
        (str(not arhivirana_vrednost), str(arhivirana_vrednost)),
        (str(arhivirana_vrednost), str(not arhivirana_vrednost)),
    ]


@pytest.mark.parametrize(
    ("nadrejeni", "otrok"),
    [
        ("Company", "Property"),
        ("Property", "Unit"),
        ("Unit", "Lease"),
        ("Tenant", "Lease"),
        ("Lease", "LeaseItem"),
        ("Company", "LeaseItem"),
    ],
)
def test_neaktiven_nadrejeni_blokira_reaktivacijo(pogodba, nadrejeni, otrok):
    veriga = cleni(pogodba)
    assert arhiviraj_poslovni_zapis(veriga[otrok]).uspesno is True
    arhiviraj_nadrejeni = arhiviraj_podjetje if nadrejeni == "Company" else arhiviraj_poslovni_zapis
    assert arhiviraj_nadrejeni(veriga[nadrejeni]).uspesno is True

    rezultat = ponovno_aktiviraj_poslovni_zapis(veriga[otrok])

    assert rezultat.uspesno is False
    assert any(razlog.koda == "NEAKTIVEN_NADREJENI" for razlog in rezultat.razlogi)
    veriga[otrok].refresh_from_db()
    assert getattr(veriga[otrok], POLJA[otrok][0]) is POLJA[otrok][1]
    assert (
        AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(type(veriga[otrok])),
            object_id=veriga[otrok].pk,
        ).count()
        == 1
    )


def test_neveljaven_zapis_ostane_arhiviran(pogodba):
    item = cleni(pogodba)["LeaseItem"]
    assert arhiviraj_poslovni_zapis(item).uspesno is True
    update_financial_fixture(LeaseItem.objects.filter(pk=item.pk), znesek_neto_mesecno=Decimal("0.00"))

    rezultat = ponovno_aktiviraj_poslovni_zapis(item)

    assert rezultat.uspesno is False
    assert any(razlog.koda == "NEVELJAVEN_ZAPIS" for razlog in rezultat.razlogi)
    item.refresh_from_db()
    assert item.arhivirana is True
    assert (
        AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(LeaseItem),
            object_id=item.pk,
        ).count()
        == 1
    )


def test_reaktivacija_starsa_ne_aktivira_otroka(pogodba):
    veriga = cleni(pogodba)
    assert arhiviraj_poslovni_zapis(veriga["LeaseItem"]).uspesno is True
    assert arhiviraj_poslovni_zapis(veriga["Lease"]).uspesno is True

    rezultat = ponovno_aktiviraj_poslovni_zapis(veriga["Lease"])

    assert rezultat.uspesno is True
    veriga["Lease"].refresh_from_db()
    veriga["LeaseItem"].refresh_from_db()
    assert veriga["Lease"].arhivirana is False
    assert veriga["LeaseItem"].arhivirana is True


def test_neobstojec_zapis_vrne_razlog(pogodba):
    item = cleni(pogodba)["LeaseItem"]
    LeaseItem.objects.filter(pk=item.pk).delete()
    rezultat = ponovno_aktiviraj_poslovni_zapis(item)
    assert rezultat.uspesno is False
    assert [razlog.koda for razlog in rezultat.razlogi] == ["ZAPIS_NE_OBSTAJA"]
