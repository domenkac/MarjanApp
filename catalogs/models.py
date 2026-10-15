from datetime import date

from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.models import FinancialQuerySet, validate_financial_save


class VatRegimeQuerySet(FinancialQuerySet):
    def bulk_update(self, objs, fields, batch_size=None):
        if "nacin_obracuna" in fields:
            raise ValidationError("Načina obračuna ni dovoljeno spreminjati. Dodajte nov DDV režim.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def update(self, **kwargs):
        if "nacin_obracuna" in kwargs:
            raise ValidationError("Načina obračuna ni dovoljeno spreminjati. Dodajte nov DDV režim.")
        return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        if "nacin_obracuna" in (kwargs.get("update_fields") or ()):
            raise ValidationError("Načina obračuna ni dovoljeno spreminjati. Dodajte nov DDV režim.")
        return super().bulk_create(objs, **kwargs)


class VatRegime(models.Model):
    objects = VatRegimeQuerySet.as_manager()

    def _validate_method(self):
        if self.pk is None:
            return
        previous = type(self).objects.filter(pk=self.pk).values_list("nacin_obracuna", flat=True).first()
        if previous is not None and previous != self.nacin_obracuna:
            raise ValidationError({"nacin_obracuna": "Načina obračuna ni dovoljeno spreminjati. Dodajte nov DDV režim."})

    def save(self, *args, **kwargs):
        edit = self.__dict__.get("_financial_edit")
        if edit is not None and self.pk is not None:
            from .services import save_vat_regime_edit

            return save_vat_regime_edit(self, source_fingerprint=edit[0], razlog=edit[1])
        with transaction.atomic():
            self._validate_method()
            validate_financial_save(self, kwargs.get("update_fields"), kwargs.get("using"))
            return super().save(*args, **kwargs)

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
        self._validate_method()
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
    objects = FinancialQuerySet.as_manager()
    business_edit_fields = (
        "naziv", "privzeta_cena", "privzeti_ddv_rezim", "enota_mere",
    )

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

    def save(self, *args, **kwargs):
        edit = self.__dict__.get("_business_edit")
        if edit is not None and self.pk is not None:
            from .services import save_service_catalog_item_edit

            return save_service_catalog_item_edit(self, source_fingerprint=edit[0], razlog=edit[1])
        with transaction.atomic(using=kwargs.get("using") or self._state.db):
            validate_financial_save(self, kwargs.get("update_fields"), kwargs.get("using"))
            return super().save(*args, **kwargs)
