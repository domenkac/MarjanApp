from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile

from companies.models import Company
from companies.services import (
    arhiviraj_poslovni_zapis,
    ponovno_aktiviraj_poslovni_zapis,
)
from core.models import AuditLogEntry
from invoicing.models import InvoiceLine
from invoicing.services import potrdi_osnutek_np, pripravi_osnutek_np
from tests.image_data import slika_bytes
from tests.invoicing.test_priprava_np import dodaj_postavko
from tests.setup_helpers import prepare_initial_setup

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def test_arhiviranje_podjetja_ohrani_zgodovino_in_izkljuci_novi_np(pogodba):
    item = dodaj_postavko(pogodba)
    company = pogodba.unit.property.company
    invoice = pripravi_osnutek_np(
        pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    prepare_initial_setup(invoice.company, invoice.lease)
    invoice = potrdi_osnutek_np(invoice)
    line = InvoiceLine.objects.get(invoice=invoice)

    rezultat = arhiviraj_poslovni_zapis(company)

    company.refresh_from_db()
    pogodba.unit.property.refresh_from_db()
    assert rezultat.uspesno is True
    assert company.aktivno is False
    assert pogodba.unit.property.aktivna is True
    assert InvoiceLine.objects.get(pk=line.pk).lease_item_id == item.pk
    invoice.refresh_from_db()
    assert (invoice.company_id, invoice.stevilka, invoice.skupaj_z_ddv) == (
        company.pk,
        "NP-00001",
        Decimal("122.00"),
    )
    assert (
        pripravi_osnutek_np(
            pogodba, date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)
        ).invoice
        is None
    )
    audit = AuditLogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(Company), object_id=company.pk
    )
    assert (audit.dejanje, audit.polje, audit.nova_vrednost) == (
        AuditLogEntry.Dejanje.SPREMEMBA,
        "aktivno",
        "False",
    )


def test_veljavna_ponovna_aktivacija_podjetja(pogodba, settings, tmp_path):
    company = pogodba.unit.property.company
    settings.MEDIA_ROOT = tmp_path
    company.logotip.save("logo.png", ContentFile(slika_bytes()), save=True)
    assert arhiviraj_poslovni_zapis(company).uspesno is True

    rezultat = ponovno_aktiviraj_poslovni_zapis(company)

    company.refresh_from_db()
    assert (rezultat.uspesno, rezultat.razlogi) == (True, ())
    assert company.aktivno is True
    revizije = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Company), object_id=company.pk
    ).order_by("pk")
    assert list(revizije.values_list("stara_vrednost", "nova_vrednost")) == [
        ("True", "False"),
        ("False", "True"),
    ]


def test_company_konflikt_davcne_blokira_reaktivacijo(pogodba, settings, tmp_path):
    company = pogodba.unit.property.company
    settings.MEDIA_ROOT = tmp_path
    company.logotip.save("logo.png", ContentFile(slika_bytes()), save=True)
    assert arhiviraj_poslovni_zapis(company).uspesno is True
    Company.objects.create(
        drzava_koda="SI",
        naziv="Drugo podjetje",
        davcna_stevilka=company.davcna_stevilka,
        maticna_stevilka="1234567000",
        naslov="Druga ulica 1",
        trr="SI56191000000123438",
        logotip="drugo.png",
        email_racunovodja="drugo@example.com",
        email_posiljatelj="drugo@example.com",
        keychain_kljuc_posiljatelja="drugo",
    )

    rezultat = ponovno_aktiviraj_poslovni_zapis(company)

    assert rezultat.uspesno is False
    assert [razlog.koda for razlog in rezultat.razlogi] == ["KONFLIKT_DAVCNE_STEVILKE"]
    company.refresh_from_db()
    assert company.aktivno is False
    assert (
        AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Company),
            object_id=company.pk,
        ).count()
        == 1
    )
