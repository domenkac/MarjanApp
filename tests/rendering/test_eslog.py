from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from django.conf import settings
from lxml import etree

from catalogs.models import VatRegime
from contracts.models import LeaseItem, Tenant
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    dodaj_vrstico,
    potrdi_korekcijski_osnutek,
    pripravi_korekcijski_osnutek,
)
from rendering.contexts import EslogRenderNapaka
from rendering.eslog import (
    NS,
    nalozi_eslog_shemo,
    pripravi_eslog_context,
    render_eslog,
    validiraj_eslog,
)

pytest_plugins = ["tests.rendering.test_contexts"]


def _vrednosti(xml_bytes, pot):
    root = etree.fromstring(xml_bytes)
    return root.xpath(pot, namespaces={"e": NS})


def _nastavi_eslog_snapshot(invoice, *, elektronski=False, kontakt=False, fields=None):
    Invoice.objects.filter(pk=invoice.pk).update(
        eslog_konfiguracija_snapshot={
            "version": "eslog-2.0-v200-config-v1",
            "segments": {
                "elektronski_naslov_kupca_vkljucen": elektronski,
                "kontaktni_podatki_kupca_vkljuceni": kontakt,
            },
            "fields": fields or {},
        }
    )


def test_minimalni_np_je_xsd_veljaven(izdani_racun):
    assert settings.FEATURE_ESLOG is False

    xml_bytes = render_eslog(izdani_racun)

    assert _vrednosti(xml_bytes, "//e:S_BGM/e:C_C106/e:D_1004/text()") == ["NP-00001"]
    assert _vrednosti(
        xml_bytes, "//e:S_DTM/e:C_C507[e:D_2005='137']/e:D_2380/text()"
    ) == ["2026-09-01"]
    assert nalozi_eslog_shemo() is not None
    assert not _vrednosti(xml_bytes, "//e:G_SG2[e:S_NAD/e:D_3035='BY']/e:G_SG5")


def test_bt49_je_samo_v_kupcevi_ic_skupini_in_xsd_veljaven(izdani_racun):
    _nastavi_eslog_snapshot(
        izdani_racun,
        elektronski=True,
        fields={"elektronski_naslov_kupca": "e-racuni@example.com"},
    )

    xml_bytes = render_eslog(izdani_racun)

    assert _vrednosti(
        xml_bytes,
        "//e:G_SG2[e:S_NAD/e:D_3035='BY']/e:G_SG5/e:S_CTA/e:D_3139/text()",
    ) == ["IC"]
    assert _vrednosti(xml_bytes, "//e:G_SG5/e:S_COM/e:C_C076/*/text()") == [
        "e-racuni@example.com",
        "EM",
    ]
    assert not _vrednosti(
        xml_bytes, "//e:G_SG2[e:S_NAD/e:D_3035='SE']/e:G_SG5"
    )
    validiraj_eslog(xml_bytes)


@pytest.mark.parametrize(
    ("fields", "cta", "com"),
    [
        ({"kontaktna_tocka_kupca": "Nabava"}, ["Nabava"], []),
        ({"kontaktni_telefon_kupca": "+386 1 234 56 78"}, [], ["TE"]),
        ({"kontaktni_email_kupca": "nabava@example.com"}, [], ["EM"]),
        (
            {
                "kontaktna_tocka_kupca": "Nabava",
                "kontaktni_telefon_kupca": "+386 1 234 56 78",
                "kontaktni_email_kupca": "nabava@example.com",
            },
            ["Nabava"],
            ["TE", "EM"],
        ),
    ],
)
def test_bg9_variacije_so_ena_pd_skupina_in_xsd_veljavne(
    izdani_racun, fields, cta, com
):
    _nastavi_eslog_snapshot(izdani_racun, kontakt=True, fields=fields)

    xml_bytes = render_eslog(izdani_racun)
    osnova = "//e:G_SG2[e:S_NAD/e:D_3035='BY']/e:G_SG5"

    assert _vrednosti(xml_bytes, osnova + "/e:S_CTA/e:D_3139/text()") == ["PD"]
    assert _vrednosti(xml_bytes, osnova + "/e:S_CTA/e:C_C056/e:D_3412/text()") == cta
    assert _vrednosti(xml_bytes, osnova + "/e:S_COM/e:C_C076/e:D_3155/text()") == com
    assert len(_vrednosti(xml_bytes, osnova)) == 1
    assert not _vrednosti(xml_bytes, osnova + "//*[not(text()) and not(*)]")
    validiraj_eslog(xml_bytes)


def test_ic_pred_pd_in_bytes_so_deterministicni(izdani_racun):
    _nastavi_eslog_snapshot(
        izdani_racun,
        elektronski=True,
        kontakt=True,
        fields={
            "elektronski_naslov_kupca": "e-racuni@example.com",
            "kontaktni_email_kupca": "nabava@example.com",
        },
    )

    prvi = render_eslog(izdani_racun)
    drugi = render_eslog(izdani_racun)

    assert prvi == drugi
    assert _vrednosti(
        prvi,
        "//e:G_SG2[e:S_NAD/e:D_3035='BY']/e:G_SG5/e:S_CTA/e:D_3139/text()",
    ) == ["IC", "PD"]


def test_sprememba_zive_company_in_tenant_konfiguracije_ne_spremeni_xml(
    izdani_racun,
):
    prvi = render_eslog(izdani_racun)
    company = izdani_racun.company
    tenant = izdani_racun.lease.tenant
    company.eslog_privzeti_neobvezni_segmenti = {
        "elektronski_naslov_kupca_vkljucen": True,
        "kontaktni_podatki_kupca_vkljuceni": True,
    }
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezni_segmenti_override = {
        "elektronski_naslov_kupca_vkljucen": True,
        "kontaktni_podatki_kupca_vkljuceni": True,
    }
    tenant.eslog_neobvezna_polja = {
        "elektronski_naslov_kupca": "nov@example.com",
        "kontaktna_tocka_kupca": "Nova nabava",
    }
    tenant.save()

    assert render_eslog(izdani_racun) == prvi


def test_context_je_nespremenljiv_in_uporablja_samo_snapshote(izdani_racun):
    context = pripravi_eslog_context(izdani_racun)
    tenant = izdani_racun.lease.tenant
    company = izdani_racun.company
    regime = LeaseItem.objects.get(lease=izdani_racun.lease).vat_regime
    item = LeaseItem.objects.get(lease=izdani_racun.lease)
    tenant.naziv = "Spremenjen kupec"
    tenant.save(update_fields=["naziv"])
    company.naziv = "Spremenjen prodajalec"
    company.save(update_fields=["naziv"])
    regime.eslog_koda = "E"
    regime.save(update_fields=["eslog_koda"])
    item.naziv_postavke = "Spremenjena postavka"
    item.save(update_fields=["naziv_postavke"])
    company.eslog_privzeti_neobvezni_segmenti = {
        "elektronski_naslov_kupca_vkljucen": True,
        "kontaktni_podatki_kupca_vkljuceni": False,
    }
    company.save(update_fields=["eslog_privzeti_neobvezni_segmenti"])
    tenant.eslog_neobvezni_segmenti_override = {
        "kontaktni_podatki_kupca_vkljuceni": True
    }
    tenant.eslog_neobvezna_polja = {"kontaktna_tocka_kupca": "Nova nabava"}
    tenant.save(
        update_fields=[
            "eslog_neobvezni_segmenti_override",
            "eslog_neobvezna_polja",
        ]
    )

    assert pripravi_eslog_context(izdani_racun) == context
    assert not context.eslog_konfiguracija.elektronski_naslov_kupca_vkljucen
    assert not context.eslog_konfiguracija.kontaktni_podatki_kupca_vkljuceni
    with pytest.raises(FrozenInstanceError):
        context.stevilka = "druga"
    with pytest.raises(FrozenInstanceError):
        context.eslog_konfiguracija.kontaktna_tocka_kupca = "Druga"


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {
            "version": "prihodnja",
            "segments": {
                "elektronski_naslov_kupca_vkljucen": False,
                "kontaktni_podatki_kupca_vkljuceni": False,
            },
            "fields": {},
        },
        {
            "version": "eslog-2.0-v200-config-v1",
            "segments": {"elektronski_naslov_kupca_vkljucen": False},
            "fields": {},
        },
        {
            "version": "eslog-2.0-v200-config-v1",
            "segments": {
                "elektronski_naslov_kupca_vkljucen": True,
                "kontaktni_podatki_kupca_vkljuceni": False,
            },
            "fields": {},
        },
    ],
)
def test_render_neveljavnega_snapshota_odpove_s_stabilno_kodo(
    izdani_racun, snapshot
):
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        eslog_konfiguracija_snapshot=snapshot
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_KONFIGURACIJA_SNAPSHOT_NEVELJAVEN"
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("koda", ["S", "E", "AE", "O"])
def test_ddv_kategorije_v_vrstici_in_rekapitulaciji(izdani_racun, koda):
    line = InvoiceLine.objects.get(invoice=izdani_racun)
    line.ddv_eslog_koda = koda
    line.ddv_stopnja = Decimal("22.00") if koda == "S" else Decimal("0.00")
    line.ddv_znesek = Decimal("22.00") if koda == "S" else Decimal("0.00")
    line.skupaj = line.osnova + line.ddv_znesek
    line.ddv_klavzula = "Potrjena klavzula" if koda in {"E", "AE"} else ""
    line.save(
        update_fields=[
            "ddv_eslog_koda",
            "ddv_stopnja",
            "ddv_znesek",
            "skupaj",
            "ddv_klavzula",
        ]
    )
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        ddv_skupaj=line.ddv_znesek, skupaj_z_ddv=line.skupaj
    )

    xml_bytes = render_eslog(izdani_racun)

    assert _vrednosti(xml_bytes, "//e:G_SG34/e:S_TAX/e:D_5305/text()") == [koda]
    assert _vrednosti(xml_bytes, "//e:G_SG52/e:S_TAX/e:D_5305/text()") == [koda]


def test_referenca_pogodbe_bt12_ct(izdani_racun):
    xml_bytes = render_eslog(izdani_racun)

    assert _vrednosti(
        xml_bytes,
        "//e:G_SG1[e:S_RFF/e:C_C506/e:D_1153='CT']/e:S_RFF/e:C_C506/e:D_1154/text()",
    ) == ["12/2024"]


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.DOBROPIS, Invoice.DocumentType.STORNO]
)
def test_korekcija_381_in_bt25_bt26(izdani_racun, tip):
    osnutek = pripravi_korekcijski_osnutek(
        izdani_racun, tip, "Popravek", izdani_racun.datum_izdaje
    )
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            osnutek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna=VatRegime.NacinObracuna.NI_PREDMET,
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI",
        )
    popravek = potrdi_korekcijski_osnutek(osnutek)

    xml_bytes = render_eslog(popravek)

    assert _vrednosti(xml_bytes, "//e:S_BGM/e:C_C002/e:D_1001/text()") == ["381"]
    assert _vrednosti(
        xml_bytes,
        "//e:G_SG1[e:S_RFF/e:C_C506/e:D_1153='OI']/e:S_RFF/e:C_C506/e:D_1154/text()",
    ) == ["NP-00001"]
    assert _vrednosti(
        xml_bytes,
        "//e:G_SG1[e:S_RFF/e:C_C506/e:D_1153='OI']/e:S_DTM/e:C_C507[e:D_2005='384']/e:D_2380/text()",
    ) == ["2026-09-01"]
    assert not _vrednosti(xml_bytes, "//e:G_SG8")


def test_korekcija_uporabi_svoj_konfiguracijski_snapshot(izdani_racun):
    _nastavi_eslog_snapshot(
        izdani_racun,
        elektronski=True,
        fields={"elektronski_naslov_kupca": "izvirnik@example.com"},
    )
    tenant = izdani_racun.lease.tenant
    tenant.eslog_neobvezni_segmenti_override = {
        "kontaktni_podatki_kupca_vkljuceni": True
    }
    tenant.eslog_neobvezna_polja = {"kontaktna_tocka_kupca": "Korekcije"}
    tenant.save()
    osnutek = pripravi_korekcijski_osnutek(
        izdani_racun, Invoice.DocumentType.STORNO, "Popravek", izdani_racun.datum_izdaje
    )
    popravek = potrdi_korekcijski_osnutek(osnutek)

    original_xml = render_eslog(izdani_racun)
    popravek_xml = render_eslog(popravek)

    assert _vrednosti(original_xml, "//e:G_SG5/e:S_CTA/e:D_3139/text()") == ["IC"]
    assert _vrednosti(popravek_xml, "//e:G_SG5/e:S_CTA/e:D_3139/text()") == ["PD"]


def test_xsd_zavrne_neveljaven_xml():
    with pytest.raises(EslogRenderNapaka) as error:
        validiraj_eslog(b"<Invoice xmlns='urn:eslog:2.00'/>")

    assert error.value.koda == "ESLOG_XSD_NEVELJAVEN"


def test_parser_ne_nalaga_zunanjih_entitet(tmp_path):
    skrivnost = tmp_path / "skrivnost.txt"
    skrivnost.write_text("NE_SME_BITI_PREBRANO", encoding="utf-8")
    xml = (
        "<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM '"
        + skrivnost.as_uri()
        + "'>]><Invoice xmlns='urn:eslog:2.00'>&xxe;</Invoice>"
    ).encode()

    with pytest.raises(EslogRenderNapaka) as error:
        validiraj_eslog(xml)

    assert "NE_SME_BITI_PREBRANO" not in str(error.value)


def test_shema_zavrne_omrezni_import(tmp_path, monkeypatch):
    xsd = tmp_path / "napadalna.xsd"
    xsd.write_text(
        "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
        "<xsd:include schemaLocation='https://example.invalid/a.xsd'/>"
        "</xsd:schema>",
        encoding="utf-8",
    )
    monkeypatch.setattr("rendering.eslog._XSD_PATH", xsd)

    with pytest.raises(EslogRenderNapaka) as error:
        nalozi_eslog_shemo()

    assert error.value.koda == "ESLOG_SHEMA_NEVELJAVNA"


def test_fizicna_oseba_brez_davcne_odpove_samo_pri_renderju(izdani_racun):
    invoice_before = Invoice.objects.get(pk=izdani_racun.pk)
    Tenant.objects.filter(pk=izdani_racun.lease.tenant_id).update(
        tip=Tenant.Tip.FIZICNA_OSEBA, davcna_stevilka=""
    )
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        prejemnik_tip="FIZICNA_OSEBA", prejemnik_davcna_stevilka=""
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_FIZICNA_OSEBA_BREZ_DAVCNE"
    invoice_after = Invoice.objects.get(pk=izdani_racun.pk)
    assert invoice_after.stevilka == invoice_before.stevilka
    assert invoice_after.rendered_eslog_path == ""
    assert InvoiceLine.objects.filter(invoice=izdani_racun).count() == 1


def test_partnerji_uporabljajo_snapshote_va_ahp_0199_in_drzavo(izdani_racun):
    xml_bytes = render_eslog(izdani_racun)

    for tip in ("SE", "BY"):
        osnova = f"//e:G_SG2[e:S_NAD/e:D_3035='{tip}']"
        assert _vrednosti(xml_bytes, osnova + "/e:S_NAD/e:D_3207/text()") == ["SI"]
        assert set(
            _vrednosti(xml_bytes, osnova + "/e:G_SG3/e:S_RFF/e:C_C506/e:D_1153/text()")
        ) == {
            "VA",
            "AHP",
            "0199",
        }

    izdani_racun.company.ddv_zavezanec = False
    izdani_racun.company.drzava_koda = "AT"
    izdani_racun.company.save(update_fields=["ddv_zavezanec", "drzava_koda"])
    izdani_racun.lease.tenant.identificiran_za_ddv = False
    izdani_racun.lease.tenant.drzava_koda = "HR"
    izdani_racun.lease.tenant.save(
        update_fields=["identificiran_za_ddv", "drzava_koda"]
    )
    assert render_eslog(izdani_racun) == xml_bytes


def test_kategorija_o_tudi_pri_identificiranih_partnerjih_ne_zapise_va(izdani_racun):
    vrstica = InvoiceLine.objects.get(invoice=izdani_racun)
    vrstica.ddv_eslog_koda = "O"
    vrstica.ddv_stopnja = Decimal("0.00")
    vrstica.ddv_znesek = Decimal("0.00")
    vrstica.skupaj = vrstica.osnova
    vrstica.save()
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        ddv_skupaj=Decimal("0.00"), skupaj_z_ddv=vrstica.osnova
    )

    kvalifikatorji = _vrednosti(
        render_eslog(izdani_racun),
        "//e:G_SG2/e:G_SG3/e:S_RFF/e:C_C506/e:D_1153/text()",
    )
    assert "VA" not in kvalifikatorji
    assert kvalifikatorji.count("AHP") == 2


def test_agregirana_vrstica_uporabi_eno_c62_in_osnovo_kot_ceno(izdani_racun):
    xml_bytes = render_eslog(izdani_racun)

    assert _vrednosti(xml_bytes, "//e:S_QTY/e:C_C186/e:D_6060/text()") == ["1"]
    assert _vrednosti(xml_bytes, "//e:S_QTY/e:C_C186/e:D_6411/text()") == ["C62"]
    assert _vrednosti(xml_bytes, "//e:G_SG29/e:S_PRI/e:C_C509/e:D_5118/text()") == [
        "100.00"
    ]


def test_neidentificirana_partnerja_zapiseta_samo_ahp(izdani_racun):
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        prejemnik_identificiran_za_ddv=False,
        izdajatelj_identificiran_za_ddv=False,
    )

    kvalifikatorji = _vrednosti(
        render_eslog(izdani_racun),
        "//e:G_SG2/e:G_SG3/e:S_RFF/e:C_C506/e:D_1153/text()",
    )
    assert "VA" not in kvalifikatorji
    assert kvalifikatorji.count("AHP") == 2


def test_tuj_prejemnik_uporabi_polni_snapshot_vat_id_brez_predpone(izdani_racun):
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        prejemnik_drzava_koda="AT",
        prejemnik_davcna_stevilka="123456789",
        prejemnik_ddv_identifikacijska_stevilka="ATU12345678",
    )

    xml_bytes = render_eslog(izdani_racun)
    osnova = "//e:G_SG2[e:S_NAD/e:D_3035='BY']/e:G_SG3/e:S_RFF/e:C_C506"
    assert _vrednosti(
        xml_bytes, osnova + "[e:D_1153='VA']/e:D_1154/text()"
    ) == ["ATU12345678"]
    assert _vrednosti(
        xml_bytes, osnova + "[e:D_1153='AHP']/e:D_1154/text()"
    ) == ["123456789"]


def test_tuj_ddv_identificiran_snapshot_brez_vat_id_odpove(izdani_racun):
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        prejemnik_drzava_koda="AT",
        prejemnik_ddv_identifikacijska_stevilka="",
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "DDV_IDENTIFIKATOR_MANJKA"


@pytest.mark.parametrize("koda", ["E", "AE"])
def test_e_in_ae_zapiseta_snapshot_klavzulo_v_bt120(izdani_racun, koda):
    klavzula = f"Klavzula {koda} iz snapshota"
    vrstica = InvoiceLine.objects.get(invoice=izdani_racun)
    vrstica.ddv_eslog_koda = koda
    vrstica.ddv_stopnja = Decimal("0.00")
    vrstica.ddv_znesek = Decimal("0.00")
    vrstica.skupaj = vrstica.osnova
    vrstica.ddv_klavzula = klavzula
    vrstica.save()
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        ddv_skupaj=Decimal("0.00"), skupaj_z_ddv=vrstica.osnova
    )

    assert _vrednosti(
        render_eslog(izdani_racun),
        "//e:S_FTX[e:D_4451='AGM']/e:C_C108/e:D_4440/text()",
    ) == [klavzula]


def test_omrezni_xsd_z_dovoljenim_imenom_ni_preusmerjen_lokalno(tmp_path, monkeypatch):
    xsd = tmp_path / "napadalna.xsd"
    xsd.write_text(
        "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
        "<xsd:include schemaLocation='https://example.invalid/xmldsig-core-schema.xsd'/>"
        "</xsd:schema>",
        encoding="utf-8",
    )
    monkeypatch.setattr("rendering.eslog._XSD_PATH", xsd)

    with pytest.raises(EslogRenderNapaka) as error:
        nalozi_eslog_shemo()

    assert error.value.koda == "ESLOG_SHEMA_NEVELJAVNA"


def test_render_zavrne_snapshot_z_mesanim_o_in_s(izdani_racun):
    prva = InvoiceLine.objects.get(invoice=izdani_racun)
    druga = InvoiceLine.objects.create(
        invoice=izdani_racun,
        opis="Transakcija zunaj DDV",
        osnova=Decimal("10.00"),
        ddv_stopnja=Decimal("0.00"),
        ddv_znesek=Decimal("0.00"),
        skupaj=Decimal("10.00"),
        ddv_nacin_obracuna=VatRegime.NacinObracuna.NI_PREDMET,
        ddv_rekap_skupina="NI_PREDMET",
        ddv_eslog_koda="O",
    )
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        osnova_skupaj=prva.osnova + druga.osnova,
        ddv_skupaj=prva.ddv_znesek + druga.ddv_znesek,
        skupaj_z_ddv=prva.skupaj + druga.skupaj,
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "DDV_ESLOG_KOMBINACIJA_NEVELJAVNA"
