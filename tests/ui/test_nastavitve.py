from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import NoReverseMatch, reverse

from companies.models import Company, Signatory
from core.services import record_edit_fingerprint
from invoicing.models import MonthlyBatch, MonthlyCycle
from invoicing.services import shrani_prvi_korak
from tests.image_data import slika_bytes


def company_data(**changes):
    data = {
        "naziv": "Alfa d.o.o.",
        "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000",
        "naslov": "Glavna ulica 1",
        "drzava_koda": "SI",
        "trr": "SI56 1910 0000 0123 438",
        "logotip": SimpleUploadedFile("logo.png", slika_bytes()),
        "ddv_zavezanec": "on",
        "email_racunovodja": "racunovodja@example.com",
        "email_posiljatelj": "racuni@example.com",
        "keychain_kljuc_posiljatelja": "marjanapp-alfa",
        "rok_placila_dni_privzeto": "15",
    }
    data.update(changes)
    return data


def create_company(**changes):
    data = company_data(logotip="logo.png")
    data.pop("ddv_zavezanec")
    data["ddv_zavezanec"] = True
    data["rok_placila_dni_privzeto"] = int(data["rok_placila_dni_privzeto"])
    data.update(changes)
    return Company.objects.create(**data)


@pytest.mark.django_db
def test_sidebar_links_to_settings_and_list_distinguishes_status(client):
    create_company()
    create_company(
        naziv="Arhiv d.o.o.", davcna_stevilka="25012550", aktivno=False
    )

    dashboard = client.get("/").content.decode()
    response = client.get("/nastavitve/")
    content = response.content.decode()

    assert 'href="/nastavitve/"' in dashboard
    assert response.status_code == 200
    assert content.index("Alfa d.o.o.") < content.index("Arhiv d.o.o.")
    assert "Aktivno" in content and "Arhivirano" in content
    assert "+ Dodaj podjetje" in content


@pytest.mark.django_db
def test_company_create_renders_saves_and_hides_internal_fields(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    response = client.get("/nastavitve/podjetja/novo/")
    content = response.content.decode()

    assert response.status_code == 200
    for hidden in (
        "logotip_checksum",
        "ddv_razporeditev_potrjena",
        "privzeti_dostavni_kanal",
        "eslog_privzeti_neobvezni_segmenti",
        "zadnja_stevilka",
        "fingerprint",
    ):
        assert hidden not in content

    response = client.post("/nastavitve/podjetja/novo/", company_data())
    company = Company.objects.get()
    assert response.status_code == 302
    assert response.url == f"/nastavitve/podjetja/{company.pk}/"
    assert company.trr == "SI56191000000123438"
    assert company.privzeti_dostavni_kanal == Company.DostavniKanal.PDF
    assert company.logotip.name.startswith("slike/")
    assert company.naziv in client.get("/nastavitve/").content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("davcna_stevilka", "15012558", "Neveljavna davčna številka"),
        ("maticna_stevilka", "123", "Matična številka mora vsebovati"),
        ("trr", "SI56191000000123439", "Neveljaven IBAN"),
        ("drzava_koda", "XX", "Koda države mora biti veljavna"),
    ],
)
def test_company_validation_is_inline(client, field, value, message):
    response = client.post(
        "/nastavitve/podjetja/novo/", company_data(**{field: value})
    )
    assert response.status_code == 200
    assert message in response.content.decode()
    assert Company.objects.count() == 0


@pytest.mark.django_db
def test_duplicate_tax_and_invalid_logo_are_inline(client):
    create_company()
    duplicate = client.post(
        "/nastavitve/podjetja/novo/",
        company_data(naziv="Drugo podjetje", logotip=SimpleUploadedFile("x.png", slika_bytes())),
    )
    invalid_image = client.post(
        "/nastavitve/podjetja/novo/",
        company_data(
            davcna_stevilka="25012550",
            logotip=SimpleUploadedFile("x.png", b"ni slika"),
        ),
    )
    assert "Aktivno podjetje s to davčno številko že obstaja" in duplicate.content.decode()
    assert "JPG ali PNG" in invalid_image.content.decode()


@pytest.mark.django_db
def test_company_edit_preserves_hidden_delivery_value(client):
    company = create_company(privzeti_dostavni_kanal=Company.DostavniKanal.OBOJE)
    data = company_data(naziv="Spremenjeno d.o.o.")
    data.pop("logotip")
    data.update(edit_fingerprint=record_edit_fingerprint(company), razlog="Popravek naziva")
    response = client.post(f"/nastavitve/podjetja/{company.pk}/", data)
    company.refresh_from_db()
    assert response.status_code == 302
    assert company.naziv == "Spremenjeno d.o.o."
    assert company.privzeti_dostavni_kanal == Company.DostavniKanal.OBOJE


@pytest.mark.django_db
def test_archive_reactivate_and_conflict_are_controlled(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company = create_company()
    company.logotip.save("logo.png", SimpleUploadedFile("logo.png", slika_bytes()), save=True)
    archive = client.post(f"/nastavitve/podjetja/{company.pk}/arhiviraj/")
    company.refresh_from_db()
    assert archive.status_code == 302 and company.aktivno is False

    reactivate = client.post(f"/nastavitve/podjetja/{company.pk}/reaktiviraj/")
    company.refresh_from_db()
    assert reactivate.status_code == 302 and company.aktivno is True

    client.post(f"/nastavitve/podjetja/{company.pk}/arhiviraj/")
    create_company(naziv="Drugo", davcna_stevilka=company.davcna_stevilka)
    conflict = client.post(f"/nastavitve/podjetja/{company.pk}/reaktiviraj/")
    assert conflict.status_code == 200
    assert "Aktivno podjetje s to davčno številko že obstaja" in conflict.content.decode()


@pytest.mark.django_db(transaction=True)
def test_settings_changes_do_not_change_cycle_membership(client):
    first = create_company()
    cycle = shrani_prvi_korak(date(2031, 2, 1), date(2031, 2, 12))
    before = list(MonthlyBatch.objects.filter(cycle=cycle).values_list("company_id", flat=True))
    client.post(
        "/nastavitve/podjetja/novo/",
        company_data(naziv="Novo", davcna_stevilka="25012550"),
    )
    client.post(f"/nastavitve/podjetja/{first.pk}/arhiviraj/")
    assert list(MonthlyBatch.objects.filter(cycle=cycle).values_list("company_id", flat=True)) == before
    assert MonthlyCycle.objects.get(pk=cycle.pk).batches.count() == 1


@pytest.mark.django_db
def test_signatory_history_create_upload_dates_and_edit(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company = create_company()
    historical = Signatory.objects.create(
        company=company,
        ime_priimek="Stari Podpisnik",
        podpis_slika="stari.png",
        velja_od=date(2020, 1, 1),
        velja_do=date(2025, 12, 31),
    )
    detail = client.get(f"/nastavitve/podjetja/{company.pk}/").content.decode()
    assert "Stari Podpisnik" in detail and "31. 12. 2025" in detail
    assert client.get(f"/nastavitve/podjetja/{company.pk}/podpisniki/nov/").status_code == 200

    response = client.post(
        f"/nastavitve/podjetja/{company.pk}/podpisniki/nov/",
        {
            "ime_priimek": "Ana Novak",
            "funkcija": "Direktorica",
            "podpis_slika": SimpleUploadedFile("podpis.png", slika_bytes()),
            "velja_od": "2026-01-01",
            "velja_do": "",
        },
    )
    current = Signatory.objects.exclude(pk=historical.pk).get()
    assert response.status_code == 302
    assert current.podpis_slika.name.startswith("slike/")
    assert current.velja_od == date(2026, 1, 1)

    edit = client.post(
        f"/nastavitve/podpisniki/{current.pk}/",
        {
            "edit_fingerprint": record_edit_fingerprint(current), "razlog": "Popravek",
            "ime_priimek": "Ana Kovač",
            "funkcija": "Direktorica",
            "velja_od": "2026-02-01",
            "velja_do": "",
        },
    )
    current.refresh_from_db()
    assert edit.status_code == 302
    assert (current.ime_priimek, current.velja_od) == ("Ana Kovač", date(2026, 2, 1))


@pytest.mark.django_db
def test_signatory_rejects_invalid_image_and_hides_checksum(client):
    company = create_company()
    url = f"/nastavitve/podjetja/{company.pk}/podpisniki/nov/"
    assert "podpis_checksum" not in client.get(url).content.decode()
    response = client.post(
        url,
        {
            "ime_priimek": "Ana Novak",
            "podpis_slika": SimpleUploadedFile("x.png", b"ni slika"),
            "velja_od": "2026-01-01",
            "velja_do": "",
        },
    )
    assert response.status_code == 200
    assert "JPG ali PNG" in response.content.decode()


@pytest.mark.django_db
def test_invalid_ids_return_404_and_no_delete_routes_exist(client):
    for url in (
        "/nastavitve/podjetja/999999/",
        "/nastavitve/podjetja/999999/arhiviraj/",
        "/nastavitve/podjetja/999999/reaktiviraj/",
        "/nastavitve/podjetja/999999/podpisniki/nov/",
        "/nastavitve/podpisniki/999999/",
    ):
        response = client.post(url) if "arhiviraj" in url or "reaktiviraj" in url else client.get(url)
        assert response.status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("ui:podjetje_izbrisi", args=[1])
    with pytest.raises(NoReverseMatch):
        reverse("ui:podpisnik_izbrisi", args=[1])
