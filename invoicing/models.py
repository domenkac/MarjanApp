from django.core.exceptions import ValidationError
from django.db import models


class Invoice(models.Model):
    class DocumentType(models.TextChoices):
        NP = "NP", "NP"
        ST = "ST", "ST"
        STORNO = "STORNO", "Storno"
        DOBROPIS = "DOBROPIS", "Dobropis"

    class Status(models.TextChoices):
        OSNUTEK = "OSNUTEK", "Osnutek"
        ZA_PONOVNI_PREGLED = "ZA_PONOVNI_PREGLED", "Za ponovni pregled"
        IZBRAN_ZA_POSILJANJE = "IZBRAN_ZA_POSILJANJE", "Izbran za pošiljanje"
        POSLAN = "POSLAN", "Poslan"
        NAPAKA = "NAPAKA", "Napaka"
        REZULTAT_NI_ZNAN = "REZULTAT_NI_ZNAN", "Rezultat ni znan"
        ARHIVIRAN_NI_IZDAN = "ARHIVIRAN_NI_IZDAN", "Arhiviran, ni izdan"
        STORNIRAN = "STORNIRAN", "Storniran"
        POPRAVLJEN = "POPRAVLJEN", "Popravljen"

    company = models.ForeignKey("companies.Company", on_delete=models.PROTECT)
    document_type = models.CharField(max_length=8, choices=DocumentType)
    lease = models.ForeignKey(
        "contracts.Lease", on_delete=models.PROTECT, null=True, blank=True
    )
    stevilka = models.CharField(null=True, blank=True)
    datum_izdaje = models.DateField()
    obdobje_od = models.DateField()
    obdobje_do = models.DateField()
    rok_placila = models.DateField(null=True, blank=True)

    prejemnik_naziv = models.CharField(blank=True)
    prejemnik_naslov = models.CharField(blank=True)
    prejemnik_drzava_koda = models.CharField(max_length=2, null=True, editable=False)
    prejemnik_identificiran_za_ddv = models.BooleanField(null=True, editable=False)
    prejemnik_davcna_stevilka = models.CharField(blank=True)
    prejemnik_ddv_identifikacijska_stevilka = models.CharField(
        max_length=70, blank=True, editable=False
    )
    prejemnik_maticna_stevilka = models.CharField(blank=True)
    prejemnik_tip = models.CharField(
        max_length=13,
        choices=[
            ("PRAVNA_OSEBA", "Pravna oseba"),
            ("FIZICNA_OSEBA", "Fizična oseba"),
        ],
        blank=True,
        default="",
    )
    izdajatelj_naziv = models.CharField(blank=True)
    izdajatelj_naslov = models.CharField(blank=True)
    izdajatelj_drzava_koda = models.CharField(max_length=2, null=True, editable=False)
    izdajatelj_identificiran_za_ddv = models.BooleanField(null=True, editable=False)
    izdajatelj_davcna_stevilka = models.CharField(blank=True)
    izdajatelj_maticna_stevilka = models.CharField(blank=True)
    izdajatelj_trr = models.CharField(blank=True)
    izdajatelj_logotip_slika_pot = models.CharField(blank=True, default="")
    pdf_template_version = models.CharField(blank=True, default="")
    izdajatelj_podpisnik_ime = models.CharField(blank=True)
    izdajatelj_podpisnik_funkcija = models.CharField(blank=True)
    izdajatelj_podpisnik_slika_pot = models.CharField(blank=True)
    oznaka_pogodbe = models.CharField(null=True, blank=True)
    objekt_naziv = models.CharField(null=True, blank=True)

    status = models.CharField(max_length=21, choices=Status, default=Status.OSNUTEK)
    povezan_racun = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    razlog = models.TextField(blank=True)
    razlog_arhiviranja = models.TextField(null=True, blank=True)
    osnova_skupaj = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    ddv_skupaj = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    skupaj_z_ddv = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    rendered_pdf_path = models.CharField(blank=True)
    rendered_eslog_path = models.CharField(blank=True)
    checksum = models.CharField(blank=True)
    rendered_eslog_checksum = models.CharField(blank=True, default="")

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["company", "stevilka"],
                name="uniq_invoice_company_stevilka",
            )
        ]

    def clean(self):
        super().clean()
        if (
            self.document_type
            in (
                self.DocumentType.STORNO,
                self.DocumentType.DOBROPIS,
            )
            and not (self.razlog or "").strip()
        ):
            raise ValidationError(
                {"razlog": "Razlog je obvezen za storno ali dobropis."}
            )


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    lease = models.ForeignKey(
        "contracts.Lease", on_delete=models.PROTECT, null=True, blank=True
    )
    lease_item = models.ForeignKey(
        "contracts.LeaseItem", on_delete=models.PROTECT, null=True, blank=True
    )
    opis = models.TextField()
    osnova = models.DecimalField(max_digits=10, decimal_places=2)
    ddv_stopnja = models.DecimalField(max_digits=5, decimal_places=2)
    ddv_znesek = models.DecimalField(max_digits=10, decimal_places=2)
    skupaj = models.DecimalField(max_digits=10, decimal_places=2)
    ddv_nacin_obracuna = models.CharField()
    ddv_klavzula = models.TextField(blank=True)
    ddv_rekap_skupina = models.CharField()
    ddv_eslog_koda = models.CharField(blank=True, default="")
    je_sorazmerno = models.BooleanField(default=False)
    razlog_rocnega_popravka = models.TextField(null=True, blank=True)
    izvirno_izracunana_vrednost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    def clean(self):
        super().clean()
        if (
            self.izvirno_izracunana_vrednost is not None
            and self.osnova is not None
            and self.osnova != self.izvirno_izracunana_vrednost
            and not (self.razlog_rocnega_popravka or "").strip()
        ):
            raise ValidationError(
                {"razlog_rocnega_popravka": "Razlog ročnega popravka je obvezen."}
            )
