from datetime import date

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
    zahteva_polni_odbitek = models.BooleanField(null=True)
    velja_od = models.DateField()
    velja_do = models.DateField(null=True, blank=True)
    eslog_koda = models.CharField(blank=True)
    aktiven = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        from contracts.vat_eligibility import preveri_ddv_omejitev

        try:
            preveri_ddv_omejitev(self.sifra, self.zahteva_polni_odbitek)
        except ValidationError as exc:
            raise ValidationError({"zahteva_polni_odbitek": exc.messages}) from exc
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

        if (
            not self.aktiven
            and self.pk
            and VatRegime.objects.filter(pk=self.pk, aktiven=True).exists()
        ):
            from contracts.services import efektivna_veljavnost_postavke

            danes = date.today()  # noqa: DTZ011
            postavke = self.leaseitem_set.filter(
                arhivirana=False, lease__arhivirana=False
            ).select_related("lease")
            for postavka in postavke:
                zacetek, konec = efektivna_veljavnost_postavke(postavka)
                if zacetek <= danes and (konec is None or danes <= konec):
                    raise ValidationError(
                        {"aktiven": "DDV režima ni mogoče deaktivirati med aktivno uporabo."}
                    )


ESLOG_KODE_DDV = {
    VatRegime.NacinObracuna.OBDAVCENO: "S",
    VatRegime.NacinObracuna.OPROSCENO: "E",
    VatRegime.NacinObracuna.OBRNJENA_DAVCNA: "AE",
    VatRegime.NacinObracuna.NI_PREDMET: "O",
}


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
