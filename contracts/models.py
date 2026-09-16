from django.core.exceptions import ValidationError
from django.db import models

from companies.models import Company


class Property(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    naziv = models.CharField()
    naslov = models.CharField()
    aktivna = models.BooleanField(default=True)


class Unit(models.Model):
    class TipEnote(models.TextChoices):
        PISARNA = "PISARNA", "Pisarna"
        POSLOVNI_PROSTOR = "POSLOVNI_PROSTOR", "Poslovni prostor"
        SKLADISCE = "SKLADISCE", "Skladišče"
        PARKIRNO_MESTO = "PARKIRNO_MESTO", "Parkirno mesto"
        GARAZA = "GARAZA", "Garaža"
        OPREMA = "OPREMA", "Oprema"
        DRUGO = "DRUGO", "Drugo"

    property = models.ForeignKey(Property, on_delete=models.PROTECT)
    oznaka = models.CharField()
    tip_enote = models.CharField(max_length=16, choices=TipEnote)
    povrsina_m2 = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    aktivna = models.BooleanField(default=True)


class Tenant(models.Model):
    class Tip(models.TextChoices):
        PRAVNA_OSEBA = "PRAVNA_OSEBA", "Pravna oseba"
        FIZICNA_OSEBA = "FIZICNA_OSEBA", "Fizična oseba"

    tip = models.CharField(max_length=13, choices=Tip)
    naziv = models.CharField()
    davcna_stevilka = models.CharField(blank=True)
    maticna_stevilka = models.CharField(max_length=10, blank=True)
    naslov = models.CharField()
    ddv_zavezanec_s_polnim_odbitkom = models.BooleanField(default=False)
    email = models.EmailField()
    aktiven = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        napake = {}
        davcna_stevilka = (self.davcna_stevilka or "").strip()
        maticna_stevilka = (self.maticna_stevilka or "").strip()

        if self.tip == self.Tip.PRAVNA_OSEBA:
            if not davcna_stevilka:
                napake["davcna_stevilka"] = (
                    "Za pravno osebo je davčna številka obvezna."
                )
            if not maticna_stevilka:
                napake["maticna_stevilka"] = (
                    "Za pravno osebo je matična številka obvezna."
                )
        if maticna_stevilka and not (
            len(maticna_stevilka) == 10 and maticna_stevilka.isdigit()
        ):
            napake["maticna_stevilka"] = (
                "Matična številka mora vsebovati natanko 10 mest."
            )

        if napake:
            raise ValidationError(napake)
