from datetime import date
from importlib import import_module

from django.apps import apps

from invoicing.models import Invoice

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def test_zgodovinski_backfill_dopolni_samo_ostevilcene_racune(pogodba):
    company = pogodba.unit.property.company
    company.eslog_privzeti_neobvezni_segmenti = {}
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    company_migracija = import_module(
        "companies.migrations.0006_company_eslog_privzeti_neobvezni_segmenti"
    )
    company_migracija.dopolni_eslog_privzete_segmentne_nastavitve(apps, None)
    company.refresh_from_db()
    assert company.eslog_privzeti_neobvezni_segmenti == {
        "elektronski_naslov_kupca_vkljucen": False,
        "kontaktni_podatki_kupca_vkljuceni": False,
    }

    skupno = {
        "company": pogodba.unit.property.company,
        "lease": pogodba,
        "document_type": Invoice.DocumentType.NP,
        "datum_izdaje": date(2026, 9, 1),
        "obdobje_od": date(2026, 8, 1),
        "obdobje_do": date(2026, 8, 31),
    }
    izdani = Invoice.objects.create(stevilka="NP-00001", **skupno)
    prazna_stevilka = Invoice.objects.create(stevilka="", **skupno)
    osnutek = Invoice.objects.create(**skupno)
    migracijski = Invoice.objects.create(
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN, **skupno
    )

    migracija = import_module(
        "invoicing.migrations.0005_invoice_eslog_konfiguracija_snapshot"
    )
    migracija.dopolni_izdane_racune(apps, None)

    izdani.refresh_from_db()
    prazna_stevilka.refresh_from_db()
    osnutek.refresh_from_db()
    migracijski.refresh_from_db()
    assert izdani.eslog_konfiguracija_snapshot == {
        "version": "eslog-2.0-v200-config-v1",
        "segments": {
            "elektronski_naslov_kupca_vkljucen": False,
            "kontaktni_podatki_kupca_vkljuceni": False,
        },
        "fields": {},
    }
    assert osnutek.eslog_konfiguracija_snapshot is None
    assert prazna_stevilka.eslog_konfiguracija_snapshot is None
    assert migracijski.eslog_konfiguracija_snapshot is None
