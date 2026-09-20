from django.core.exceptions import ValidationError
from django.db import models

from core.validators import (
    normaliziraj_iban,
    preveri_davcno_stevilko,
    preveri_iban,
    preveri_kodo_drzave,
    preveri_maticno_stevilko,
    preveri_sliko,
    varno_ime_slike,
)


class Company(models.Model):
    class DostavniKanal(models.TextChoices):
        PDF = "PDF", "PDF"
        ESLOG = "ESLOG", "eSLOG"
        OBOJE = "OBOJE", "Oboje"

    naziv = models.CharField()
    davcna_stevilka = models.CharField(max_length=8)
    maticna_stevilka = models.CharField(max_length=10)
    naslov = models.CharField()
    drzava_koda = models.CharField(
        max_length=2,
        validators=[preveri_kodo_drzave],
    )
    trr = models.CharField(max_length=34)
    logotip = models.FileField(upload_to=varno_ime_slike, validators=[preveri_sliko])
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

    def clean_fields(self, exclude=None):
        self.trr = normaliziraj_iban(self.trr)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        napake = {}
        self.trr = normaliziraj_iban(self.trr)

        if not preveri_davcno_stevilko(self.davcna_stevilka):
            napake["davcna_stevilka"] = (
                "Neveljavna davčna številka (napačna kontrolna števka)."
            )
        if not preveri_maticno_stevilko(self.maticna_stevilka):
            napake["maticna_stevilka"] = (
                "Matična številka mora vsebovati natanko 10 mest."
            )
        if not preveri_iban(self.trr):
            napake["trr"] = "Neveljaven IBAN."
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
            napake["davcna_stevilka"] = (
                "Aktivno podjetje s to davčno številko že obstaja."
            )

        if napake:
            raise ValidationError(napake)

    def save(self, *args, **kwargs):
        self.trr = normaliziraj_iban(self.trr)
        return super().save(*args, **kwargs)


class Signatory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    ime_priimek = models.CharField()
    funkcija = models.CharField(blank=True)
    podpis_slika = models.FileField(
        upload_to=varno_ime_slike, validators=[preveri_sliko]
    )
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

        if (
            self.velja_do is None
            and self.company_id
            and Signatory.objects.filter(
                company_id=self.company_id, velja_do__isnull=True
            )
            .exclude(pk=self.pk)
            .exists()
        ):
            napake["velja_do"] = "Podjetje že ima trenutnega podpisnika."

        if napake:
            raise ValidationError(napake)


class NumberSequence(models.Model):
    class Tip(models.TextChoices):
        NP = "NP", "NP"
        ST = "ST", "ST"

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    tip = models.CharField(max_length=2, choices=Tip)
    leto = models.IntegerField()
    zadnja_stevilka = models.IntegerField(default=0)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["company", "tip", "leto"],
                name="uniq_number_sequence_company_tip_leto",
            ),
            models.CheckConstraint(
                condition=(models.Q(tip="NP", leto=0) | models.Q(tip="ST", leto__gt=0)),
                name="number_sequence_veljaven_tip_leto",
            ),
        ]
