import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.db.models.deletion import ProtectedError

from companies.models import (
    Company,
    CompanySampleConfirmation,
    NumberSequence,
    Signatory,
    VatSettingsConfirmation,
)
from core.models import AuditLogEntry
from core.services import IzbrisOdlocitev, IzbrisRazlog, StanjeRazlog, StanjeRezultat

FINGERPRINT_DDV_VERSION = 1
FINGERPRINT_VZORCA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PotrditevVeljavnost:
    stanje: str
    potrditev: VatSettingsConfirmation | CompanySampleConfirmation | None = None


class AssetPotrditveSpremenjen(ValueError):
    pass


def _json_vrednost(vrednost):
    if isinstance(vrednost, Decimal):
        return format(vrednost, "f")
    if isinstance(vrednost, date):
        return vrednost.isoformat()
    if isinstance(vrednost, dict):
        return {kljuc: _json_vrednost(vrednost[kljuc]) for kljuc in sorted(vrednost)}
    if isinstance(vrednost, (list, tuple)):
        return [_json_vrednost(element) for element in vrednost]
    return vrednost


def _kanonicni_fingerprint(podatki: dict) -> str:
    zapis = json.dumps(
        _json_vrednost(podatki),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(zapis.encode("utf-8")).hexdigest()


def _ddv_podatki(company: Company) -> dict:
    from contracts.models import LeaseItem

    postavke = (
        LeaseItem.objects.filter(
            lease__unit__property__company=company,
            lease__arhivirana=False,
            lease__unit__aktivna=True,
            lease__unit__property__aktivna=True,
            arhivirana=False,
        )
        .select_related("lease__unit", "vat_regime")
        .order_by("pk")
    )
    return {
        "schema_version": FINGERPRINT_DDV_VERSION,
        "company": {"id": company.pk, "ddv_zavezanec": company.ddv_zavezanec},
        "allocations": [
            {
                "lease_item_id": postavka.pk,
                "lease_id": postavka.lease_id,
                "unit_id": postavka.lease.unit_id,
                "vat_regime_id": postavka.vat_regime_id,
                "vat_regime": {
                    "nacin_obracuna": postavka.vat_regime.nacin_obracuna,
                    "stopnja": postavka.vat_regime.stopnja,
                    "klavzula": postavka.vat_regime.klavzula,
                    "rekapitulacijska_skupina": (
                        postavka.vat_regime.rekapitulacijska_skupina
                    ),
                    "zahteva_referenco": postavka.vat_regime.zahteva_referenco,
                },
            }
            for postavka in postavke
        ],
    }


def izracunaj_fingerprint_ddv(company: Company) -> str:
    company = Company.objects.get(pk=company.pk)
    return _kanonicni_fingerprint(_ddv_podatki(company))


def potrdi_ddv_nastavitve(company: Company) -> VatSettingsConfirmation:
    with transaction.atomic():
        company = Company.objects.get(pk=company.pk)
        fingerprint = _kanonicni_fingerprint(_ddv_podatki(company))
        return VatSettingsConfirmation.objects.create(
            company=company,
            source_fingerprint=fingerprint,
            fingerprint_schema_version=FINGERPRINT_DDV_VERSION,
        )


def _veljavnost_ddv(company: Company, fingerprint: str) -> PotrditevVeljavnost:
    potrditve = VatSettingsConfirmation.objects.filter(company=company).order_by(
        "-confirmed_at", "-pk"
    )
    if not potrditve.exists():
        return PotrditevVeljavnost("missing")
    ujemanje = potrditve.filter(
        source_fingerprint=fingerprint,
        fingerprint_schema_version=FINGERPRINT_DDV_VERSION,
    ).first()
    if ujemanje:
        return PotrditevVeljavnost("valid", ujemanje)
    return PotrditevVeljavnost("stale")


def veljavna_potrditev_ddv(company: Company) -> PotrditevVeljavnost:
    company = Company.objects.get(pk=company.pk)
    return _veljavnost_ddv(company, izracunaj_fingerprint_ddv(company))


def _checksum_immutable_asseta(pot: str) -> str:
    immutable_pot = shrani_nespremenljiv_asset(pot)
    return immutable_pot.rsplit("/", 1)[-1]


def _vzorec_podatki(company: Company, lease, podpisnik: Signatory) -> dict:
    from contracts.models import LeaseItem

    tenant = lease.tenant
    nepremicnina = lease.unit.property
    postavke = (
        LeaseItem.objects.filter(lease=lease, arhivirana=False)
        .select_related("vat_regime")
        .order_by("pk")
    )
    return {
        "schema_version": FINGERPRINT_VZORCA_VERSION,
        "pdf_template_version": settings.PDF_TEMPLATE_VERSION,
        "vat_settings_fingerprint": izracunaj_fingerprint_ddv(company),
        "company": {
            "id": company.pk,
            "naziv": company.naziv,
            "naslov": company.naslov,
            "drzava_koda": company.drzava_koda,
            "davcna_stevilka": company.davcna_stevilka,
            "maticna_stevilka": company.maticna_stevilka,
            "ddv_zavezanec": company.ddv_zavezanec,
            "trr": company.trr,
            "logo_asset_name": company.logotip.name,
            "logo_content_sha256": company.logotip_checksum,
        },
        "property": {
            "id": nepremicnina.pk,
            "naziv": nepremicnina.naziv,
            "naslov": nepremicnina.naslov,
            "aktivna": nepremicnina.aktivna,
        },
        "unit": {
            "id": lease.unit_id,
            "oznaka": lease.unit.oznaka,
            "tip_enote": lease.unit.tip_enote,
            "povrsina_m2": lease.unit.povrsina_m2,
            "aktivna": lease.unit.aktivna,
        },
        "lease": {
            "id": lease.pk,
            "oznaka_pogodbe": lease.oznaka_pogodbe,
            "tip_trajanja": lease.tip_trajanja,
            "datum_zacetka": lease.datum_zacetka,
            "datum_konca": lease.datum_konca,
            "samodejno_podaljsanje": lease.samodejno_podaljsanje,
            "obdobje_podaljsanja_mesecev": lease.obdobje_podaljsanja_mesecev,
            "odpovedni_rok_mesecev": lease.odpovedni_rok_mesecev,
            "datum_odpovedi": lease.datum_odpovedi,
            "datum_prenehanja": lease.datum_prenehanja,
            "status": lease.status,
            "arhivirana": lease.arhivirana,
            "rok_placila_dni": lease.rok_placila_dni,
            "ima_klavzulo_indeksacije": lease.ima_klavzulo_indeksacije,
            "opis_pogoja_indeksacije": lease.opis_pogoja_indeksacije,
            "tip_pogoja_indeksacije": lease.tip_pogoja_indeksacije,
            "prag_spremembe_odstotek": lease.prag_spremembe_odstotek,
        },
        "tenant": {
            "id": tenant.pk,
            "tip": tenant.tip,
            "naziv": tenant.naziv,
            "naslov": tenant.naslov,
            "drzava_koda": tenant.drzava_koda,
            "davcna_stevilka": tenant.davcna_stevilka,
            "ddv_identifikacijska_stevilka": tenant.ddv_identifikacijska_stevilka,
            "maticna_stevilka": tenant.maticna_stevilka,
            "identificiran_za_ddv": tenant.identificiran_za_ddv,
            "ddv_zavezanec_s_polnim_odbitkom": (tenant.ddv_zavezanec_s_polnim_odbitkom),
            "aktiven": tenant.aktiven,
        },
        "signatory": {
            "id": podpisnik.pk,
            "ime_priimek": podpisnik.ime_priimek,
            "funkcija": podpisnik.funkcija,
            "velja_od": podpisnik.velja_od,
            "velja_do": podpisnik.velja_do,
            "signature_asset_name": podpisnik.podpis_slika.name,
            "signature_content_sha256": podpisnik.podpis_checksum,
        },
        "items": [
            {
                "id": postavka.pk,
                "naziv_postavke": postavka.naziv_postavke,
                "znesek_neto_mesecno": postavka.znesek_neto_mesecno,
                "referenca_listine": postavka.referenca_listine,
                "datum_reference": postavka.datum_reference,
                "datum_zacetka_postavke": postavka.datum_zacetka_postavke,
                "datum_prenehanja_postavke": postavka.datum_prenehanja_postavke,
                "indeks_ob_osnovi": postavka.indeks_ob_osnovi,
                "datum_osnove_indeksacije": postavka.datum_osnove_indeksacije,
                "osnovna_najemnina_ob_sklenitvi": (
                    postavka.osnovna_najemnina_ob_sklenitvi
                ),
                "vat_regime_id": postavka.vat_regime_id,
                "vat_nacin_obracuna": postavka.vat_regime.nacin_obracuna,
                "vat_stopnja": postavka.vat_regime.stopnja,
                "vat_klavzula": postavka.vat_regime.klavzula,
                "vat_rekapitulacijska_skupina": (
                    postavka.vat_regime.rekapitulacijska_skupina
                ),
            }
            for postavka in postavke
        ],
    }


def _nalozi_vzorec(company: Company, lease):
    from contracts.models import Lease

    company = Company.objects.get(pk=company.pk)
    lease = (
        Lease.objects.select_related("tenant", "unit__property__company")
        .filter(pk=lease.pk)
        .first()
    )
    if lease is None or lease.unit.property.company_id != company.pk:
        raise ValueError("Pogodba ne pripada podjetju.")
    podpisnik = izberi_veljavnega_podpisnika(company, date.today())  # noqa: DTZ011
    return company, lease, podpisnik


def izracunaj_fingerprint_vzorca(company: Company, lease) -> str:
    company, lease, podpisnik = _nalozi_vzorec(company, lease)
    return _kanonicni_fingerprint(_vzorec_podatki(company, lease, podpisnik))


def potrdi_vzorec_podjetja(company: Company, lease) -> CompanySampleConfirmation:
    from rendering.contexts import pripravi_vzorcni_pdf_context

    company, lease, podpisnik = _nalozi_vzorec(company, lease)
    danes = date.today()  # noqa: DTZ011
    prvi_dan = danes.replace(day=1)
    zadnji_dan = danes.replace(day=monthrange(danes.year, danes.month)[1])
    pripravi_vzorcni_pdf_context(
        lease,
        datum_izdaje=danes,
        obdobje_od=prvi_dan,
        obdobje_do=zadnji_dan,
    )
    logo_pot = company.logotip.name
    podpis_pot = podpisnik.podpis_slika.name
    logo_checksum = _checksum_immutable_asseta(logo_pot)
    podpis_checksum = _checksum_immutable_asseta(podpis_pot)

    with transaction.atomic():
        company, lease, podpisnik = _nalozi_vzorec(company, lease)
        if (
            company.logotip.name != logo_pot
            or podpisnik.podpis_slika.name != podpis_pot
        ):
            raise AssetPotrditveSpremenjen(
                "Logotip ali podpis se je med potrjevanjem spremenil; poskusite znova."
            )
        Company.objects.filter(pk=company.pk).update(logotip_checksum=logo_checksum)
        Signatory.objects.filter(pk=podpisnik.pk).update(
            podpis_checksum=podpis_checksum
        )
        company.logotip_checksum = logo_checksum
        podpisnik.podpis_checksum = podpis_checksum
        fingerprint = _kanonicni_fingerprint(_vzorec_podatki(company, lease, podpisnik))
        return CompanySampleConfirmation.objects.create(
            company=company,
            lease=lease,
            pdf_template_version=settings.PDF_TEMPLATE_VERSION,
            source_fingerprint=fingerprint,
            fingerprint_schema_version=FINGERPRINT_VZORCA_VERSION,
        )


def veljavna_potrditev_vzorca(company: Company) -> PotrditevVeljavnost:
    company = Company.objects.get(pk=company.pk)
    potrditve = CompanySampleConfirmation.objects.filter(company=company).order_by(
        "-confirmed_at", "-pk"
    )
    if not potrditve.exists():
        return PotrditevVeljavnost("missing")
    for potrditev in potrditve.select_related("lease"):
        try:
            fingerprint = izracunaj_fingerprint_vzorca(company, potrditev.lease)
        except (ValueError, OSError):
            continue
        if (
            potrditev.pdf_template_version == settings.PDF_TEMPLATE_VERSION
            and potrditev.fingerprint_schema_version == FINGERPRINT_VZORCA_VERSION
            and potrditev.source_fingerprint == fingerprint
        ):
            return PotrditevVeljavnost("valid", potrditev)
    return PotrditevVeljavnost("stale")


def veljavni_podpisniki_na_dan(company: Company, datum_izdaje):
    return Signatory.objects.filter(company=company, velja_od__lte=datum_izdaje).filter(
        Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje)
    )


def izberi_veljavnega_podpisnika(company: Company, datum_izdaje) -> Signatory:
    podpisniki = list(
        veljavni_podpisniki_na_dan(company, datum_izdaje).order_by("pk")[:2]
    )
    if not podpisniki:
        raise ValueError(
            f"Ni veljavnega podpisnika za {company.naziv} na {datum_izdaje}"
        )
    if len(podpisniki) > 1:
        raise ValueError(
            f"Več veljavnih podpisnikov za {company.naziv} na {datum_izdaje}; "
            "popravite obdobja veljavnosti"
        )
    return podpisniki[0]


def shrani_nespremenljiv_asset(pot: str) -> str:
    try:
        with default_storage.open(pot, "rb") as datoteka:
            vsebina = datoteka.read()
    except (OSError, ValueError) as napaka:
        raise ValueError(f"Asset ni dostopen: {pot}.") from napaka

    digest = sha256(vsebina).hexdigest()
    cilj = f"nespremenljivi-asseti/{digest}"

    def preveri_cilj() -> None:
        try:
            with default_storage.open(cilj, "rb") as datoteka:
                pravilen = sha256(datoteka.read()).hexdigest() == digest
        except (OSError, ValueError) as napaka:
            raise ValueError(f"Nespremenljiv asset ni dostopen: {cilj}.") from napaka
        if not pravilen:
            raise ValueError(f"Nespremenljiv asset je poškodovan: {cilj}.")

    if default_storage.exists(cilj):
        preveri_cilj()
        return cilj

    shranjena_pot = default_storage.save(cilj, ContentFile(vsebina))
    if shranjena_pot == cilj:
        preveri_cilj()
        return cilj

    try:
        preveri_cilj()
    finally:
        default_storage.delete(shranjena_pot)
    return cilj


@transaction.atomic
def dodeli_naslednjo_stevilko(company, tip, datum_izdaje):
    leto = datum_izdaje.year if tip == NumberSequence.Tip.ST else 0
    zaporedje, _ = NumberSequence.objects.get_or_create(
        company=company,
        tip=tip,
        leto=leto,
        defaults={"zadnja_stevilka": 0},
    )
    zaporedje.zadnja_stevilka = F("zadnja_stevilka") + 1
    zaporedje.save(update_fields=["zadnja_stevilka"])
    zaporedje.refresh_from_db(fields=["zadnja_stevilka"])
    return zaporedje.zadnja_stevilka


def _svezi_zapis(zapis):
    if type(zapis) is not Company:
        raise TypeError("Nepodprt tip poslovnega zapisa.")
    return Company.objects.filter(pk=zapis.pk).first() if zapis.pk else None


def _audit(zapis, stara_vrednost, nova_vrednost, razlog):
    revizija = AuditLogEntry(
        content_type=ContentType.objects.get_for_model(Company),
        object_id=zapis.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
        polje="aktivno",
        stara_vrednost=str(stara_vrednost),
        nova_vrednost=str(nova_vrednost),
        razlog=razlog,
    )
    revizija.full_clean()
    return revizija


def arhiviraj_poslovni_zapis(zapis) -> StanjeRezultat:
    with transaction.atomic():
        svezi = _svezi_zapis(zapis)
        if svezi is None:
            return StanjeRezultat(
                False, (StanjeRazlog("ZAPIS_NE_OBSTAJA", "Zapis ne obstaja."),)
            )
        if not svezi.aktivno:
            return StanjeRezultat(
                False, (StanjeRazlog("ZE_ARHIVIRAN", "Zapis je že arhiviran."),)
            )

        svezi.aktivno = False
        revizija = _audit(svezi, True, False, "Arhiviranje poslovnega zapisa")
        svezi.save(update_fields=["aktivno"])
        revizija.save()
        return StanjeRezultat(True)


def ponovno_aktiviraj_poslovni_zapis(zapis) -> StanjeRezultat:
    try:
        with transaction.atomic():
            svezi = _svezi_zapis(zapis)
            if svezi is None:
                return StanjeRezultat(
                    False, (StanjeRazlog("ZAPIS_NE_OBSTAJA", "Zapis ne obstaja."),)
                )
            if svezi.aktivno:
                return StanjeRezultat(
                    False, (StanjeRazlog("NI_ARHIVIRAN", "Zapis ni arhiviran."),)
                )

            if (
                Company.objects.filter(
                    aktivno=True, davcna_stevilka=svezi.davcna_stevilka
                )
                .exclude(pk=svezi.pk)
                .exists()
            ):
                return StanjeRezultat(
                    False,
                    (
                        StanjeRazlog(
                            "KONFLIKT_DAVCNE_STEVILKE",
                            "Aktivno podjetje s to davčno številko že obstaja.",
                        ),
                    ),
                )

            svezi.aktivno = True
            try:
                svezi.full_clean()
            except ValidationError as napaka:
                polja = getattr(napaka, "message_dict", {"__all__": napaka.messages})
                return StanjeRezultat(
                    False,
                    tuple(
                        StanjeRazlog("NEVELJAVEN_ZAPIS", f"{ime}: {sporocilo}")
                        for ime, sporocila in polja.items()
                        for sporocilo in sporocila
                    ),
                )
            except OSError:
                return StanjeRezultat(
                    False,
                    (
                        StanjeRazlog(
                            "NEVELJAVEN_ZAPIS", "Povezana datoteka ni dostopna."
                        ),
                    ),
                )

            revizija = _audit(
                svezi, False, True, "Ponovna aktivacija poslovnega zapisa"
            )
            svezi.save()
            revizija.save()
            return StanjeRezultat(True)
    except IntegrityError:
        if (
            Company.objects.filter(aktivno=True, davcna_stevilka=zapis.davcna_stevilka)
            .exclude(pk=zapis.pk)
            .exists()
        ):
            return StanjeRezultat(
                False,
                (
                    StanjeRazlog(
                        "KONFLIKT_DAVCNE_STEVILKE",
                        "Aktivno podjetje s to davčno številko že obstaja.",
                    ),
                ),
            )
        return StanjeRezultat(
            False,
            (StanjeRazlog("OMEJITEV_PODATKOV", "Podatkovna omejitev ni izpolnjena."),),
        )


def odloci_o_fizicnem_izbrisu(zapis, *, izrecna_potrditev=False) -> IzbrisOdlocitev:
    svezi = _svezi_zapis(zapis)
    if svezi is None:
        return IzbrisOdlocitev(
            False,
            (IzbrisRazlog("ZAPIS_NE_OBSTAJA", "Zapis ne obstaja.", "Company", 0),),
        )

    from contracts.models import Property
    from invoicing.models import Invoice

    razlogi = []

    def povezava(model, koda, **filter):
        stevilo = model.objects.filter(**filter).count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    koda,
                    f"Povezani zapisi {model.__name__} preprečujejo izbris.",
                    model.__name__,
                    stevilo,
                )
            )

    povezava(Signatory, "PODREJENI_ZAPISI", company_id=svezi.pk)
    povezava(Property, "PODREJENI_ZAPISI", company_id=svezi.pk)
    povezava(Invoice, "POVEZAN_RACUN", company_id=svezi.pk)
    racuni = Invoice.objects.filter(company_id=svezi.pk)
    stevilo = racuni.filter(stevilka__isnull=False).count()
    if stevilo:
        razlogi.append(
            IzbrisRazlog(
                "ZAPIS_UPORABLJEN",
                "Zapis je bil uporabljen na izdanem računu.",
                "Invoice",
                stevilo,
            )
        )
    stevilo = racuni.exclude(rendered_pdf_path="", rendered_eslog_path="").count()
    if stevilo:
        razlogi.append(
            IzbrisRazlog(
                "POVEZAN_DOKUMENT",
                "Ustvarjen dokument preprečuje izbris.",
                "Invoice",
                stevilo,
            )
        )
    stevilo = racuni.filter(status=Invoice.Status.POSLAN).count()
    if stevilo:
        razlogi.append(
            IzbrisRazlog(
                "POVEZANO_POSILJANJE",
                "Poslan račun preprečuje izbris.",
                "Invoice",
                stevilo,
            )
        )
    povezava(NumberSequence, "POVEZANO_STEVILCENJE", company_id=svezi.pk)
    povezava(
        VatSettingsConfirmation,
        "POVEZANA_POTRDITEV",
        company_id=svezi.pk,
    )
    povezava(
        CompanySampleConfirmation,
        "POVEZANA_POTRDITEV",
        company_id=svezi.pk,
    )
    if svezi.ddv_razporeditev_potrjena:
        razlogi.append(
            IzbrisRazlog(
                "POVEZANA_POTRDITEV",
                "Razporeditev DDV je že potrjena.",
                "ddv_razporeditev_potrjena",
                1,
            )
        )
    povezava(
        AuditLogEntry,
        "POSLOVNA_ZGODOVINA",
        content_type=ContentType.objects.get_for_model(Company),
        object_id=svezi.pk,
    )
    if izrecna_potrditev is not True:
        razlogi.append(
            IzbrisRazlog(
                "POTRDITEV_MANJKA",
                "Uporabnik ni izrecno potrdil fizičnega izbrisa.",
                "potrditev",
                0,
            )
        )
    return IzbrisOdlocitev(not razlogi, tuple(razlogi), bool(razlogi))


def fizicno_izbrisi_poslovni_zapis(
    zapis, *, izrecna_potrditev=False
) -> IzbrisOdlocitev:
    odlocitev = odloci_o_fizicnem_izbrisu(zapis, izrecna_potrditev=izrecna_potrditev)
    if not odlocitev.dovoljeno:
        return odlocitev

    try:
        with transaction.atomic():
            odlocitev = odloci_o_fizicnem_izbrisu(
                zapis, izrecna_potrditev=izrecna_potrditev
            )
            if not odlocitev.dovoljeno:
                return odlocitev
            svezi = _svezi_zapis(zapis)
            revizija = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(Company),
                object_id=svezi.pk,
                dejanje=AuditLogEntry.Dejanje.FIZICNI_IZBRIS,
                razlog="Uporabnik je izrecno potrdil fizični izbris neuporabljenega zapisa.",
            )
            revizija.full_clean()
            revizija.save()
            svezi.delete()
    except (ProtectedError, IntegrityError):
        odlocitev = odloci_o_fizicnem_izbrisu(
            zapis, izrecna_potrditev=izrecna_potrditev
        )
        if not odlocitev.dovoljeno:
            return odlocitev
        return IzbrisOdlocitev(
            False,
            (
                IzbrisRazlog(
                    "POSLOVNA_POVEZAVA",
                    "Podatkovna povezava preprečuje izbris.",
                    "omejitev_podatkov",
                    1,
                ),
            ),
            True,
        )
    return IzbrisOdlocitev(True)
