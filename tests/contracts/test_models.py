from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    Property,
    RecipientContact,
    Tenant,
    Unit,
)


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


def podatki_najemnika(**spremembe):
    podatki = {
        "tip": Tenant.Tip.PRAVNA_OSEBA,
        "naziv": "Najemnik d.o.o.",
        "drzava_koda": "SI",
        "identificiran_za_ddv": True,
        "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000",
        "naslov": "Trg 1",
        "email": "najemnik@example.com",
    }
    return podatki | spremembe


@pytest.mark.django_db
def test_property_in_unit_polja_privzete_vrednosti_in_natancnost():
    company = ustvari_podjetje()
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
        povrsina_m2=Decimal("42.50"),
    )

    assert nepremicnina.aktivna is True
    assert enota.aktivna is True
    povrsina = Unit._meta.get_field("povrsina_m2")
    assert (povrsina.max_digits, povrsina.decimal_places) == (8, 2)


@pytest.mark.django_db
def test_tip_enote_je_zaprt_textchoices_nabor():
    assert set(Unit.TipEnote.values) == {
        "PISARNA",
        "POSLOVNI_PROSTOR",
        "SKLADISCE",
        "PARKIRNO_MESTO",
        "GARAZA",
        "OPREMA",
        "DRUGO",
    }
    nepremicnina = Property.objects.create(
        company=ustvari_podjetje(),
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit(property=nepremicnina, oznaka="X", tip_enote="NEVELJAVNO")

    with pytest.raises(ValidationError):
        enota.full_clean()


@pytest.mark.django_db
def test_poslovni_fk_preprecijo_kaskadni_izbris():
    company = ustvari_podjetje()
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
    )

    with pytest.raises(ProtectedError):
        company.delete()
    with pytest.raises(ProtectedError):
        nepremicnina.delete()


@pytest.mark.django_db
def test_pravna_oseba_zahteva_davcno_in_maticno_stevilko():
    najemnik = Tenant(**podatki_najemnika(davcna_stevilka="", maticna_stevilka=""))

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert set(napaka.value.message_dict) == {
        "davcna_stevilka",
        "maticna_stevilka",
    }


@pytest.mark.django_db
def test_fizicna_oseba_ne_zahteva_davcne_in_maticne_stevilke():
    najemnik = Tenant(
        **podatki_najemnika(
            tip=Tenant.Tip.FIZICNA_OSEBA,
            naziv="Ana Novak",
            identificiran_za_ddv=False,
            davcna_stevilka="",
            maticna_stevilka="",
        )
    )

    najemnik.full_clean()


@pytest.mark.django_db
def test_maticna_stevilka_mora_imeti_natanko_deset_mest():
    najemnik = Tenant(**podatki_najemnika(maticna_stevilka="123456789"))

    with pytest.raises(ValidationError, match="natanko 10 mest"):
        najemnik.full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize("tip", [Tenant.Tip.PRAVNA_OSEBA, Tenant.Tip.FIZICNA_OSEBA])
@pytest.mark.parametrize("davcna", ["15012558", "1501255", "1501255A"])
def test_izpolnjena_neveljavna_davcna_je_zavrnjena_za_oba_tipa(tip, davcna):
    najemnik = Tenant(**podatki_najemnika(tip=tip, davcna_stevilka=davcna))

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert "davcna_stevilka" in napaka.value.message_dict


@pytest.mark.django_db
def test_veljavna_davcna_pravne_osebe_je_sprejeta():
    Tenant(**podatki_najemnika()).full_clean()


@pytest.mark.django_db
def test_tuja_pravna_oseba_ne_uporabi_slovenskih_formatov():
    Tenant(
        **podatki_najemnika(
            drzava_koda="AT",
            davcna_stevilka="U12345678",
            ddv_identifikacijska_stevilka="ATU12345678",
            maticna_stevilka="FN-123",
        )
    ).full_clean()


@pytest.mark.django_db
def test_tuja_pravna_oseba_dovoljuje_daljso_registracijsko_stevilko():
    Tenant(
        **podatki_najemnika(
            drzava_koda="AT",
            davcna_stevilka="U12345678",
            ddv_identifikacijska_stevilka="ATU12345678",
            maticna_stevilka="FN-123456789012345",
        )
    ).full_clean()


@pytest.mark.django_db
def test_tuja_pravna_oseba_se_vedno_zahteva_davcni_ali_ddv_identifikator():
    najemnik = Tenant(
        **podatki_najemnika(
            drzava_koda="AT",
            davcna_stevilka="",
            maticna_stevilka="",
        )
    )

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert "davcna_stevilka" in napaka.value.message_dict
    assert "maticna_stevilka" not in napaka.value.message_dict


@pytest.mark.django_db
def test_tuj_ddv_zavezanec_zahteva_polno_ddv_identifikacijsko_stevilko():
    najemnik = Tenant(
        **podatki_najemnika(
            drzava_koda="AT",
            davcna_stevilka="123456789",
            ddv_identifikacijska_stevilka="",
        )
    )

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert "ddv_identifikacijska_stevilka" in napaka.value.message_dict


@pytest.mark.django_db
def test_tenant_tip_je_zaprt_textchoices_nabor_in_email_je_veljaven():
    assert set(Tenant.Tip.values) == {"PRAVNA_OSEBA", "FIZICNA_OSEBA"}
    najemnik = Tenant(**podatki_najemnika(tip="NEVELJAVNO", email="ni-email"))

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert {"tip", "email"} <= set(napaka.value.message_dict)


def ustvari_pogodbeni_stranki(rok_placila_dni=15):
    company = ustvari_podjetje()
    company.rok_placila_dni_privzeto = rok_placila_dni
    company.save(update_fields=["rok_placila_dni_privzeto"])
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
    )
    najemnik = Tenant.objects.create(**podatki_najemnika())
    return enota, najemnik


def nova_pogodba(**spremembe):
    enota, najemnik = ustvari_pogodbeni_stranki()
    podatki = {
        "unit": enota,
        "tenant": najemnik,
        "oznaka_pogodbe": "P-2026-001",
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": date(2026, 1, 1),
    }
    return Lease(**(podatki | spremembe))


@pytest.mark.django_db
def test_lease_textchoices_in_privzete_vrednosti():
    assert set(Lease.TipTrajanja.values) == {"DOLOCEN_CAS", "NEDOLOCEN_CAS"}
    assert set(Lease.Status.values) == {"AKTIVNA", "ODPOVEDANA", "PRENEHALA"}
    assert set(Lease.TipPogojaIndeksacije.values) == {
        "VEDNO",
        "PRAG_SPREMEMBE",
        "SAMO_NAVZGOR",
    }

    pogodba = nova_pogodba()
    pogodba.full_clean()

    assert pogodba.samodejno_podaljsanje is False
    assert pogodba.status == Lease.Status.AKTIVNA
    assert pogodba.arhivirana is False
    assert pogodba.ima_klavzulo_indeksacije is False


@pytest.mark.django_db
def test_rok_placila_ob_ustvarjanju_privzame_podjetje_in_ostane_nastavljiv():
    enota, najemnik = ustvari_pogodbeni_stranki(rok_placila_dni=22)
    skupni = {
        "unit": enota,
        "tenant": najemnik,
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": date(2026, 1, 1),
    }
    pogodba = Lease.objects.create(oznaka_pogodbe="P-1", **skupni)
    rocna = Lease.objects.create(oznaka_pogodbe="P-2", rok_placila_dni=30, **skupni)

    assert pogodba.rok_placila_dni == 22
    assert rocna.rok_placila_dni == 30


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tip_trajanja", "datum_konca"),
    [
        (Lease.TipTrajanja.DOLOCEN_CAS, None),
        (Lease.TipTrajanja.NEDOLOCEN_CAS, date(2026, 12, 31)),
    ],
)
def test_tip_trajanja_doloca_datum_konca(tip_trajanja, datum_konca):
    pogodba = nova_pogodba(
        tip_trajanja=tip_trajanja,
        datum_konca=datum_konca,
    )

    with pytest.raises(ValidationError) as napaka:
        pogodba.full_clean()

    assert "datum_konca" in napaka.value.message_dict


@pytest.mark.django_db
def test_samodejno_podaljsanje_zahteva_obdobje():
    pogodba = nova_pogodba(samodejno_podaljsanje=True)

    with pytest.raises(ValidationError) as napaka:
        pogodba.full_clean()

    assert "obdobje_podaljsanja_mesecev" in napaka.value.message_dict


@pytest.mark.django_db
def test_indeksacijska_klavzula_zahteva_tip_in_prag_kadar_ustreza():
    brez_tipa = nova_pogodba(ima_klavzulo_indeksacije=True)
    with pytest.raises(ValidationError) as napaka:
        brez_tipa.full_clean()
    assert "tip_pogoja_indeksacije" in napaka.value.message_dict

    brez_tipa.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    with pytest.raises(ValidationError) as napaka:
        brez_tipa.full_clean()
    assert "prag_spremembe_odstotek" in napaka.value.message_dict

    brez_tipa.prag_spremembe_odstotek = Decimal("2.00")
    brez_tipa.full_clean()


@pytest.mark.django_db
def test_datum_prenehanja_status_arhiviranje_in_poslovna_fk():
    pogodba = nova_pogodba(
        datum_prenehanja=date(2026, 6, 30),
        status=Lease.Status.AKTIVNA,
        arhivirana=True,
    )
    pogodba.full_clean()
    pogodba.save()

    assert pogodba.status == Lease.Status.AKTIVNA
    assert pogodba.arhivirana is True
    with pytest.raises(ProtectedError):
        pogodba.unit.delete()
    with pytest.raises(ProtectedError):
        pogodba.tenant.delete()


def ustvari_rezim(zahteva_referenco=False):
    return VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="DDV22",
        naziv="DDV 22 %",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="DDV 22 %",
        zahteva_referenco=zahteva_referenco,
        velja_od=date(2026, 1, 1),
    )


def nova_postavka(zahteva_referenco=False, **spremembe):
    pogodba = nova_pogodba()
    pogodba.save()
    rezim = ustvari_rezim(zahteva_referenco=zahteva_referenco)
    podatki = {
        "lease": pogodba,
        "vat_regime": rezim,
        "znesek_neto_mesecno": Decimal("500.00"),
    }
    return LeaseItem(**(podatki | spremembe))


@pytest.mark.django_db
def test_lease_item_polja_privzete_vrednosti_in_pozitiven_znesek():
    postavka = nova_postavka()
    postavka.full_clean()
    postavka.save()

    assert postavka.naziv_postavke == "Najemnina"
    assert postavka.arhivirana is False
    assert postavka.datum_zacetka_postavke is None
    assert postavka.datum_prenehanja_postavke is None
    assert not hasattr(postavka, "aktivna")
    assert (
        LeaseItem._meta.get_field("znesek_neto_mesecno").max_digits,
        LeaseItem._meta.get_field("znesek_neto_mesecno").decimal_places,
    ) == (10, 2)

    postavka.znesek_neto_mesecno = Decimal("0.00")
    with pytest.raises(ValidationError) as napaka:
        postavka.full_clean()
    assert "znesek_neto_mesecno" in napaka.value.message_dict

    postavka.znesek_neto_mesecno = Decimal("-1.00")
    with pytest.raises(ValidationError):
        postavka.full_clean()


@pytest.mark.django_db
def test_referenca_listine_in_datum_sta_obvezna_le_ce_ju_zahteva_rezim():
    postavka = nova_postavka(zahteva_referenco=True)

    with pytest.raises(ValidationError) as napaka:
        postavka.full_clean()
    assert {"referenca_listine", "datum_reference"} <= set(napaka.value.message_dict)

    postavka.referenca_listine = "Pogodba P-1"
    with pytest.raises(ValidationError) as napaka:
        postavka.full_clean()
    assert "datum_reference" in napaka.value.message_dict

    postavka.datum_reference = date(2026, 1, 1)
    postavka.full_clean()


@pytest.mark.django_db
def test_indeksacijska_klavzula_zahteva_oba_izhodiscna_podatka():
    postavka = nova_postavka()
    postavka.lease.ima_klavzulo_indeksacije = True
    postavka.lease.save(update_fields=["ima_klavzulo_indeksacije"])

    with pytest.raises(ValidationError) as napaka:
        postavka.full_clean()
    assert {"indeks_ob_osnovi", "datum_osnove_indeksacije"} <= set(
        napaka.value.message_dict
    )

    postavka.indeks_ob_osnovi = Decimal("100.000")
    with pytest.raises(ValidationError) as napaka:
        postavka.full_clean()
    assert "datum_osnove_indeksacije" in napaka.value.message_dict

    postavka.datum_osnove_indeksacije = date(2026, 1, 1)
    postavka.full_clean()


@pytest.mark.django_db
def test_lease_item_datumi_in_arhiv_so_loceni_in_fk_sciteni():
    postavka = nova_postavka(
        datum_zacetka_postavke=date(2026, 2, 1),
        datum_prenehanja_postavke=date(2026, 12, 31),
        arhivirana=True,
        osnovna_najemnina_ob_sklenitvi=Decimal("450.00"),
    )
    postavka.full_clean()
    postavka.save()

    assert postavka.arhivirana is True
    with pytest.raises(ProtectedError):
        postavka.lease.delete()
    with pytest.raises(ProtectedError):
        postavka.vat_regime.delete()


@pytest.mark.django_db
@pytest.mark.parametrize("vloga", RecipientContact.Vloga.values)
@pytest.mark.parametrize("kanal", RecipientContact.Kanal.values)
def test_recipient_contact_pravna_oseba_dovoljuje_vse_vloge_in_kanale(vloga, kanal):
    pogodba = nova_pogodba()
    pogodba.save()
    kontakt = RecipientContact(
        lease=pogodba,
        email="prejemnik@example.com",
        vloga=vloga,
        kanal=kanal,
    )

    kontakt.full_clean()
    kontakt.save()
    assert kontakt.aktiven is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kanal", "dovoljen"),
    [
        (RecipientContact.Kanal.PDF, True),
        (RecipientContact.Kanal.ESLOG, False),
        (RecipientContact.Kanal.OBOJE, False),
    ],
)
def test_recipient_contact_fizicna_oseba_samo_pdf(kanal, dovoljen):
    pogodba = nova_pogodba()
    pogodba.tenant.tip = Tenant.Tip.FIZICNA_OSEBA
    pogodba.tenant.save(update_fields=["tip"])
    pogodba.save()
    kontakt = RecipientContact(
        lease=pogodba,
        email="ana@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=kanal,
    )

    if dovoljen:
        kontakt.full_clean()
        kontakt.save()
        with pytest.raises(ProtectedError):
            pogodba.delete()
    else:
        with pytest.raises(ValidationError) as napaka:
            kontakt.full_clean()
        assert "kanal" in napaka.value.message_dict


@pytest.mark.django_db
def test_recipient_contact_zavrne_neveljavne_izbire_in_email():
    pogodba = nova_pogodba()
    pogodba.save()
    kontakt = RecipientContact(
        lease=pogodba,
        email="neveljavno",
        vloga="NEVELJAVNO",
        kanal="NEVELJAVNO",
    )

    with pytest.raises(ValidationError) as napaka:
        kontakt.full_clean()

    assert {"email", "vloga", "kanal"} <= set(napaka.value.message_dict)


@pytest.mark.django_db
def test_indexation_event_polja_privzete_vrednosti_in_fk():
    postavka = nova_postavka()
    postavka.save()
    dogodek = IndexationEvent(
        lease_item=postavka,
        datum_uskladitve=date(2026, 2, 1),
        indeks_vrednost=Decimal("103.200"),
        stara_vrednost=Decimal("500.00"),
        nova_vrednost=Decimal("516.00"),
    )
    dogodek.full_clean()
    dogodek.save()

    assert dogodek.izkljucen is False
    assert (
        IndexationEvent._meta.get_field("indeks_vrednost").max_digits,
        IndexationEvent._meta.get_field("indeks_vrednost").decimal_places,
    ) == (6, 3)
    with pytest.raises(ProtectedError):
        postavka.delete()


@pytest.mark.django_db
def test_indexation_event_zahteva_datum_in_razlog_samo_ob_izkljucitvi():
    postavka = nova_postavka()
    postavka.save()
    dogodek = IndexationEvent(
        lease_item=postavka,
        indeks_vrednost=Decimal("103.200"),
        stara_vrednost=Decimal("500.00"),
        nova_vrednost=Decimal("516.00"),
    )

    with pytest.raises(ValidationError) as napaka:
        dogodek.full_clean()
    assert "datum_uskladitve" in napaka.value.message_dict

    dogodek.datum_uskladitve = date(2026, 2, 1)
    dogodek.izkljucen = True
    with pytest.raises(ValidationError) as napaka:
        dogodek.full_clean()
    assert "razlog_izkljucitve" in napaka.value.message_dict

    dogodek.razlog_izkljucitve = "Ročna izključitev"
    dogodek.full_clean()


def aktivna_postavka():
    postavka = nova_postavka()
    postavka.lease.datum_zacetka = date.today() - timedelta(days=30)  # noqa: DTZ011
    postavka.lease.save(update_fields=["datum_zacetka"])
    postavka.save()
    return postavka


@pytest.mark.django_db
def test_aktivna_postavka_blokira_samo_deaktivacijo():
    rezim = aktivna_postavka().vat_regime
    rezim.full_clean()

    rezim.aktiven = False
    with pytest.raises(ValidationError) as napaka:
        rezim.full_clean()

    assert "aktiven" in napaka.value.message_dict


@pytest.mark.django_db
@pytest.mark.parametrize("arhiviran", ["postavka", "pogodba"])
def test_arhivirana_postavka_ali_pogodba_ne_blokira(arhiviran):
    postavka = aktivna_postavka()
    zapis = postavka if arhiviran == "postavka" else postavka.lease
    zapis.arhivirana = True
    zapis.save(update_fields=["arhivirana"])

    rezim = postavka.vat_regime
    rezim.aktiven = False
    rezim.full_clean()


@pytest.mark.django_db
def test_neuporabljen_rezim_se_lahko_deaktivira():
    rezim = ustvari_rezim()
    rezim.aktiven = False
    rezim.full_clean()


@pytest.mark.django_db
def test_ze_neaktiven_rezim_in_reaktivacija_nista_blokirana():
    rezim = aktivna_postavka().vat_regime
    VatRegime.objects.filter(pk=rezim.pk).update(aktiven=False)
    rezim.refresh_from_db()
    rezim.full_clean()

    rezim.aktiven = True
    rezim.full_clean()


@pytest.mark.django_db
def test_drug_rezim_na_isti_pogodbi_ne_blokira():
    postavka = aktivna_postavka()
    postavka.arhivirana = True
    postavka.save(update_fields=["arhivirana"])
    drug_rezim = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="DDV9",
        naziv="DDV 9 %",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("9.50"),
        rekapitulacijska_skupina="DDV 9 %",
        velja_od=date(2026, 1, 1),
    )
    LeaseItem.objects.create(
        lease=postavka.lease,
        vat_regime=drug_rezim,
        znesek_neto_mesecno=Decimal("100.00"),
    )

    rezim = postavka.vat_regime
    rezim.aktiven = False
    rezim.full_clean()


@pytest.mark.django_db
def test_ena_aktivna_postavka_med_vec_zadostuje_za_blokado():
    postavka = aktivna_postavka()
    postavka.arhivirana = True
    postavka.save(update_fields=["arhivirana"])
    LeaseItem.objects.create(
        lease=postavka.lease,
        vat_regime=postavka.vat_regime,
        znesek_neto_mesecno=Decimal("100.00"),
    )

    rezim = postavka.vat_regime
    rezim.aktiven = False
    with pytest.raises(ValidationError):
        rezim.full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("polje", "odmik", "status", "blokira"),
    [
        ("datum_prenehanja_postavke", -1, None, False),
        ("datum_zacetka_postavke", 1, None, False),
        ("datum_prenehanja_postavke", 0, None, True),
        ("datum_prenehanja", -1, Lease.Status.PRENEHALA, False),
        ("datum_prenehanja", 1, Lease.Status.ODPOVEDANA, True),
        ("datum_prenehanja", -1, Lease.Status.AKTIVNA, False),
        ("datum_prenehanja", 1, Lease.Status.PRENEHALA, True),
        ("datum_konca", -1, Lease.Status.AKTIVNA, False),
        ("datum_konca", 0, Lease.Status.AKTIVNA, True),
        ("datum_konca", 1, Lease.Status.AKTIVNA, True),
        ("datum_zacetka", 1, Lease.Status.AKTIVNA, False),
        ("datum_zacetka", 0, Lease.Status.AKTIVNA, True),
    ],
)
def test_deaktivacija_uposteva_efektivno_veljavnost(polje, odmik, status, blokira):
    postavka = aktivna_postavka()
    zapis = postavka if "postavke" in polje else postavka.lease
    setattr(zapis, polje, date.today() + timedelta(days=odmik))  # noqa: DTZ011
    polja = [polje]
    if status is not None:
        zapis.status = status
        polja.append("status")
    if polje == "datum_konca":
        zapis.tip_trajanja = Lease.TipTrajanja.DOLOCEN_CAS
        polja.append("tip_trajanja")
    zapis.save(update_fields=polja)

    rezim = postavka.vat_regime
    rezim.aktiven = False
    if blokira:
        with pytest.raises(ValidationError):
            rezim.full_clean()
    else:
        rezim.full_clean()


@pytest.mark.django_db
def test_identificiran_za_ddv_zahteva_davcno_stevilko():
    najemnik = Tenant(**podatki_najemnika(davcna_stevilka=""))

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert "davcna_stevilka" in napaka.value.message_dict


@pytest.mark.django_db
def test_polni_odbitek_ni_dovoljen_brez_identifikacije_za_ddv():
    najemnik = Tenant(
        **podatki_najemnika(
            identificiran_za_ddv=False,
            ddv_zavezanec_s_polnim_odbitkom=True,
        )
    )

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert "ddv_zavezanec_s_polnim_odbitkom" in napaka.value.message_dict
