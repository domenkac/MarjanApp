import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.eslog_konfiguracija import (
    SEGMENTI_V1,
    EslogKonfiguracijaNapaka,
    normaliziraj_segmentne_nastavitve,
    privzeti_segmenti_podjetja_v1,
)
from core.models import FinancialQuerySet, validate_financial_save
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
    objects = FinancialQuerySet.as_manager()
    financial_fields = (
        "naziv", "davcna_stevilka", "maticna_stevilka", "naslov", "drzava_koda",
        "trr", "ddv_zavezanec", "rok_placila_dni_privzeto", "aktivno",
    )

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
    logotip_checksum = models.CharField(max_length=64, blank=True, editable=False)
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
    eslog_privzeti_neobvezni_segmenti = models.JSONField(
        default=privzeti_segmenti_podjetja_v1
    )

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
        try:
            segmenti = normaliziraj_segmentne_nastavitve(
                self.eslog_privzeti_neobvezni_segmenti
            )
            if set(segmenti) != set(SEGMENTI_V1):
                raise EslogKonfiguracijaNapaka(
                    "ESLOG_PRIVZETI_SEGMENTI_MANJKAJO",
                    "Podjetje mora določiti vse podprte eSLOG segmente.",
                )
        except EslogKonfiguracijaNapaka as exc:
            napake["eslog_privzeti_neobvezni_segmenti"] = str(exc)

        if napake:
            raise ValidationError(napake)

    def save(self, *args, **kwargs):
        edit = self.__dict__.get("_financial_edit")
        if edit is not None and self.pk is not None:
            from .services import save_company_edit

            return save_company_edit(self, source_fingerprint=edit[0], razlog=edit[1])
        with transaction.atomic(using=kwargs.get("using") or self._state.db):
            self.trr = normaliziraj_iban(self.trr)
            validate_financial_save(self, kwargs.get("update_fields"), kwargs.get("using"))
            return super().save(*args, **kwargs)


class Signatory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    ime_priimek = models.CharField()
    funkcija = models.CharField(blank=True)
    podpis_slika = models.FileField(
        upload_to=varno_ime_slike, validators=[preveri_sliko]
    )
    podpis_checksum = models.CharField(max_length=64, blank=True, editable=False)
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


class VatSettingsConfirmation(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="vat_settings_confirmations",
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)
    source_fingerprint = models.CharField(max_length=64)
    fingerprint_schema_version = models.PositiveSmallIntegerField()

    class Meta:
        indexes = [  # noqa: RUF012
            models.Index(fields=["company", "-confirmed_at"]),
        ]


class CompanySampleConfirmation(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="sample_confirmations",
    )
    lease = models.ForeignKey(
        "contracts.Lease",
        on_delete=models.PROTECT,
        related_name="sample_confirmations",
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)
    pdf_template_version = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64)
    fingerprint_schema_version = models.PositiveSmallIntegerField()
    preview = models.ForeignKey(
        "SamplePdfPreview", null=True, on_delete=models.PROTECT,
        related_name="confirmations",
    )

    class Meta:
        indexes = [  # noqa: RUF012
            models.Index(fields=["company", "-confirmed_at"]),
        ]


class SamplePdfPreview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    lease = models.ForeignKey("contracts.Lease", on_delete=models.PROTECT)
    rendered_at = models.DateTimeField(auto_now_add=True)
    source_fingerprint = models.CharField(max_length=64)
    fingerprint_schema_version = models.PositiveSmallIntegerField()
    pdf_template_version = models.CharField(max_length=64)
    pdf_checksum = models.CharField(max_length=64)
    content_fingerprint = models.CharField(max_length=64)
    datum_izdaje = models.DateField()
    obdobje_od = models.DateField()
    obdobje_do = models.DateField()


class NumberingVerification(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    verified_at = models.DateTimeField(auto_now_add=True)
    np_last = models.PositiveIntegerField()
    st_last = models.PositiveIntegerField()
    st_year = models.PositiveSmallIntegerField()


class InitialSetupCompletion(models.Model):
    completed_at = models.DateTimeField(auto_now_add=True)
    evidence = models.JSONField()

    class Meta:
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=models.Q(id=1), name="initial_setup_completion_singleton"
            ),
        ]
