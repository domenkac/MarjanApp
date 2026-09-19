from hashlib import sha256

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.db.models.deletion import ProtectedError

from companies.models import Company, NumberSequence, Signatory
from core.models import AuditLogEntry
from core.services import IzbrisOdlocitev, IzbrisRazlog, StanjeRazlog, StanjeRezultat


def veljavni_podpisniki_na_dan(company: Company, datum_izdaje):
    return Signatory.objects.filter(
        company=company, velja_od__lte=datum_izdaje
    ).filter(Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje))


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
    if default_storage.exists(cilj):
        with default_storage.open(cilj, "rb") as datoteka:
            if sha256(datoteka.read()).hexdigest() != digest:
                raise ValueError(f"Nespremenljiv asset je poškodovan: {cilj}.")
        return cilj

    shranjena_pot = default_storage.save(cilj, ContentFile(vsebina))
    if shranjena_pot != cilj:
        raise ValueError("Nespremenljivega asseta ni bilo mogoče varno shraniti.")
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
