import re

from django.core.exceptions import ValidationError
from django.db import models

_IBAN_SPECS = """
AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BR29 BY28
CH21 CR22 CY28 CZ24 DE22 DK18 DO28 EE20 EG29 ES24 FI18
FO18 FR27 GB22 GE22 GI23 GL18 GR27 GT28 HR21 HU28 IE22
IL23 IQ23 IS26 IT27 JO30 KZ20 LB28 LC32 LI21 LT20 LU20
LV21 MC27 MD24 ME22 MK19 MR27 MT31 MU30 NL18 NO15 OM23
PK24 PL28 PS29 PT25 QA29 RO24 RS22 SA24 SC31 SE24 SI19
SK24 SM27 ST25 SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20
""".split()  # noqa: SIM905
IBAN_LENGTHS = {item[:2]: int(item[2:]) for item in _IBAN_SPECS}


def _veljavna_davcna_stevilka(stevilka):
    if not (len(stevilka) == 8 and stevilka.isdigit()):
        return False
    vsota = sum(int(cifra) * ponder for cifra, ponder in zip(stevilka[:7], range(8, 1, -1)))
    ostanek = vsota % 11
    if ostanek == 0:
        return False
    kontrolna = 11 - ostanek
    return (0 if kontrolna == 10 else kontrolna) == int(stevilka[7])


def _normaliziraj_iban(iban):
    return "".join(iban.split()).upper()


def _veljaven_iban(iban):
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", iban):
        return False
    if len(iban) != IBAN_LENGTHS.get(iban[:2]):
        return False
    prestavljeno = iban[4:] + iban[:4]
    stevilsko = "".join(znak if znak.isdigit() else str(ord(znak) - 55) for znak in prestavljeno)
    return int(stevilsko) % 97 == 1


def _veljaven_logotip(logotip):
    polozaj = logotip.tell()
    glava = logotip.read(8)
    logotip.seek(polozaj)
    return glava.startswith(b"\xff\xd8\xff") or glava == b"\x89PNG\r\n\x1a\n"


class Company(models.Model):
    class DostavniKanal(models.TextChoices):
        PDF = "PDF", "PDF"
        ESLOG = "ESLOG", "eSLOG"
        OBOJE = "OBOJE", "Oboje"

    naziv = models.CharField()
    davcna_stevilka = models.CharField(max_length=8)
    maticna_stevilka = models.CharField(max_length=10)
    naslov = models.CharField()
    trr = models.CharField(max_length=34)
    logotip = models.FileField()
    ddv_zavezanec = models.BooleanField(default=True)
    email_racunovodja = models.EmailField()
    email_posiljatelj = models.EmailField()
    keychain_kljuc_posiljatelja = models.CharField()
    privzeti_dostavni_kanal = models.CharField(
        max_length=5,
        choices=DostavniKanal,
        default=DostavniKanal.PDF,
    )
    rok_placila_dni_privzeto = models.IntegerField(default=15)
    ddv_razporeditev_potrjena = models.BooleanField(default=False)
    aktivno = models.BooleanField(default=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["davcna_stevilka"],
                condition=models.Q(aktivno=True),
                name="uniq_active_company_davcna_stevilka",
            )
        ]

    def clean(self):
        super().clean()
        napake = {}
        self.trr = _normaliziraj_iban(self.trr)

        if not _veljavna_davcna_stevilka(self.davcna_stevilka):
            napake["davcna_stevilka"] = "Neveljavna davčna številka (napačna kontrolna števka)."
        if not (len(self.maticna_stevilka) == 10 and self.maticna_stevilka.isdigit()):
            napake["maticna_stevilka"] = "Matična številka mora vsebovati natanko 10 mest."
        if not _veljaven_iban(self.trr):
            napake["trr"] = "Neveljaven IBAN."
        if self.logotip and not _veljaven_logotip(self.logotip):
            napake["logotip"] = "Logotip mora biti datoteka JPG ali PNG."
        if (
            self.aktivno
            and self.davcna_stevilka
            and Company.objects.filter(
                aktivno=True,
                davcna_stevilka=self.davcna_stevilka,
            )
            .exclude(pk=self.pk)
            .exists()
        ):
            napake["davcna_stevilka"] = "Aktivno podjetje s to davčno številko že obstaja."

        if napake:
            raise ValidationError(napake)

    def save(self, *args, **kwargs):
        self.trr = _normaliziraj_iban(self.trr)
        return super().save(*args, **kwargs)


class Signatory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    ime_priimek = models.CharField()
    funkcija = models.CharField(blank=True)
    podpis_slika = models.FileField()
    velja_od = models.DateField()
    velja_do = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(velja_do__isnull=True),
                name="uniq_current_signatory_per_company",
            )
        ]

    def clean(self):
        super().clean()
        napake = {}

        if self.podpis_slika and not _veljaven_logotip(self.podpis_slika):
            napake["podpis_slika"] = "Podpis mora biti datoteka JPG ali PNG."
        if (
            self.velja_do is None
            and self.company_id
            and Signatory.objects.filter(company_id=self.company_id, velja_do__isnull=True)
            .exclude(pk=self.pk)
            .exists()
        ):
            napake["velja_do"] = "Podjetje že ima trenutnega podpisnika."

        if napake:
            raise ValidationError(napake)