from django.core.exceptions import ValidationError
from django.db import models


class VatRegime(models.Model):
    class NacinObracuna(models.TextChoices):
        OBDAVCENO = "OBDAVCENO", "Obdavčeno"
        OPROSCENO = "OPROSCENO", "Oproščeno"
        NI_PREDMET = "NI_PREDMET", "Ni predmet"
        OBRNJENA_DAVCNA = "OBRNJENA_DAVCNA", "Obrnjena davčna"

    sifra = models.CharField(unique=True)
    naziv = models.CharField()
    nacin_obracuna = models.CharField(max_length=15, choices=NacinObracuna)
    stopnja = models.DecimalField(max_digits=5, decimal_places=2)
    klavzula = models.TextField(blank=True)
    rekapitulacijska_skupina = models.CharField()
    zahteva_referenco = models.BooleanField(default=False)
    velja_od = models.DateField()
    velja_do = models.DateField(null=True, blank=True)
    eslog_koda = models.CharField(blank=True)
    aktiven = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if (
            self.nacin_obracuna
            in {
                self.NacinObracuna.OPROSCENO,
                self.NacinObracuna.OBRNJENA_DAVCNA,
            }
            and not (self.klavzula or "").strip()
        ):
            raise ValidationError(
                {"klavzula": "Klavzula je obvezna za izbrani način obračuna."}
            )


class ServiceCatalogItem(models.Model):
    naziv = models.CharField()
    privzeta_cena = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    privzeti_ddv_rezim = models.ForeignKey(
        VatRegime,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    enota_mere = models.CharField()
    aktivna = models.BooleanField(default=True)
