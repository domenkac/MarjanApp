from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from catalogs.models import VatRegime
from companies.models import Company
from core.validators import (
    preveri_davcno_stevilko,
    preveri_kodo_drzave,
    preveri_maticno_stevilko,
)


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
    davcna_stevilka = models.CharField(max_length=70, blank=True)
    ddv_identifikacijska_stevilka = models.CharField(max_length=70, blank=True)
    maticna_stevilka = models.CharField(max_length=70, blank=True)
    naslov = models.CharField()
    drzava_koda = models.CharField(
        max_length=2,
        validators=[preveri_kodo_drzave],
    )
    identificiran_za_ddv = models.BooleanField()
    ddv_zavezanec_s_polnim_odbitkom = models.BooleanField(default=False)
    email = models.EmailField()
    aktiven = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        napake = {}
        davcna_stevilka = (self.davcna_stevilka or "").strip()
        ddv_identifikacijska_stevilka = (
            self.ddv_identifikacijska_stevilka or ""
        ).strip()
        maticna_stevilka = (self.maticna_stevilka or "").strip()

        if self.identificiran_za_ddv and not davcna_stevilka:
            napake["davcna_stevilka"] = (
                "Davčna številka je obvezna za osebo, identificirano za DDV."
            )
        if (
            self.identificiran_za_ddv
            and self.drzava_koda != "SI"
            and not ddv_identifikacijska_stevilka
        ):
            napake["ddv_identifikacijska_stevilka"] = (
                "Tuja oseba, identificirana za DDV, mora imeti DDV identifikacijsko "
                "številko."
            )
        if self.ddv_zavezanec_s_polnim_odbitkom and self.identificiran_za_ddv is False:
            napake["ddv_zavezanec_s_polnim_odbitkom"] = (
                "Polni odbitek DDV zahteva identifikacijo za DDV."
            )
        je_slovenski = self.drzava_koda == "SI"
        if self.tip == self.Tip.PRAVNA_OSEBA:
            if not davcna_stevilka:
                napake["davcna_stevilka"] = (
                    "Za pravno osebo je davčna številka obvezna."
                )
            if je_slovenski and not maticna_stevilka:
                napake["maticna_stevilka"] = (
                    "Za pravno osebo je matična številka obvezna."
                )
        if (
            je_slovenski
            and davcna_stevilka
            and not preveri_davcno_stevilko(davcna_stevilka)
        ):
            napake["davcna_stevilka"] = (
                "Neveljavna davčna številka (napačna kontrolna števka)."
            )
        if (
            je_slovenski
            and maticna_stevilka
            and not preveri_maticno_stevilko(maticna_stevilka)
        ):
            napake["maticna_stevilka"] = (
                "Matična številka mora vsebovati natanko 10 mest."
            )

        if napake:
            raise ValidationError(napake)


class Lease(models.Model):
    class TipTrajanja(models.TextChoices):
        DOLOCEN_CAS = "DOLOCEN_CAS", "Določen čas"
        NEDOLOCEN_CAS = "NEDOLOCEN_CAS", "Nedoločen čas"

    class Status(models.TextChoices):
        AKTIVNA = "AKTIVNA", "Aktivna"
        ODPOVEDANA = "ODPOVEDANA", "Odpovedana"
        PRENEHALA = "PRENEHALA", "Prenehala"

    class TipPogojaIndeksacije(models.TextChoices):
        VEDNO = "VEDNO", "Vedno"
        PRAG_SPREMEMBE = "PRAG_SPREMEMBE", "Prag spremembe"
        SAMO_NAVZGOR = "SAMO_NAVZGOR", "Samo navzgor"

    unit = models.ForeignKey(Unit, on_delete=models.PROTECT)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    oznaka_pogodbe = models.CharField()
    tip_trajanja = models.CharField(max_length=13, choices=TipTrajanja)
    datum_zacetka = models.DateField()
    datum_konca = models.DateField(null=True, blank=True)
    samodejno_podaljsanje = models.BooleanField(default=False)
    obdobje_podaljsanja_mesecev = models.IntegerField(null=True, blank=True)
    odpovedni_rok_mesecev = models.IntegerField(null=True, blank=True)
    datum_odpovedi = models.DateField(null=True, blank=True)
    datum_prenehanja = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status,
        default=Status.AKTIVNA,
    )
    arhivirana = models.BooleanField(default=False)
    rok_placila_dni = models.IntegerField()
    ima_klavzulo_indeksacije = models.BooleanField(default=False)
    opis_pogoja_indeksacije = models.TextField(blank=True)
    tip_pogoja_indeksacije = models.CharField(
        max_length=14,
        choices=TipPogojaIndeksacije,
        blank=True,
    )
    prag_spremembe_odstotek = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )

    def _nastavi_privzeti_rok_placila(self):
        if self._state.adding and self.rok_placila_dni is None and self.unit_id:
            self.rok_placila_dni = (
                Unit.objects.filter(pk=self.unit_id)
                .values_list(
                    "property__company__rok_placila_dni_privzeto",
                    flat=True,
                )
                .first()
            )

    def clean_fields(self, exclude=None):
        self._nastavi_privzeti_rok_placila()
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        napake = {}

        if (
            self.tip_trajanja == self.TipTrajanja.DOLOCEN_CAS
            and self.datum_konca is None
        ):
            napake["datum_konca"] = "Za pogodbo za določen čas je datum konca obvezen."
        elif (
            self.tip_trajanja == self.TipTrajanja.NEDOLOCEN_CAS
            and self.datum_konca is not None
        ):
            napake["datum_konca"] = (
                "Pogodba za nedoločen čas ne sme imeti datuma konca."
            )

        if self.samodejno_podaljsanje and self.obdobje_podaljsanja_mesecev is None:
            napake["obdobje_podaljsanja_mesecev"] = (
                "Obdobje podaljšanja je obvezno pri samodejnem podaljšanju."
            )

        if self.ima_klavzulo_indeksacije and not self.tip_pogoja_indeksacije:
            napake["tip_pogoja_indeksacije"] = (
                "Tip pogoja je obvezen pri indeksacijski klavzuli."
            )
        if (
            self.tip_pogoja_indeksacije == self.TipPogojaIndeksacije.PRAG_SPREMEMBE
            and self.prag_spremembe_odstotek is None
        ):
            napake["prag_spremembe_odstotek"] = (
                "Prag spremembe je obvezen za ta tip indeksacije."
            )

        if napake:
            raise ValidationError(napake)

    def save(self, *args, **kwargs):
        self._nastavi_privzeti_rok_placila()
        return super().save(*args, **kwargs)


class LeaseItem(models.Model):
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT)
    naziv_postavke = models.CharField(default="Najemnina")
    znesek_neto_mesecno = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    vat_regime = models.ForeignKey(VatRegime, on_delete=models.PROTECT)
    referenca_listine = models.CharField(blank=True)
    datum_reference = models.DateField(null=True, blank=True)
    datum_zacetka_postavke = models.DateField(null=True, blank=True)
    datum_prenehanja_postavke = models.DateField(null=True, blank=True)
    arhivirana = models.BooleanField(default=False)
    indeks_ob_osnovi = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        null=True,
        blank=True,
    )
    datum_osnove_indeksacije = models.DateField(null=True, blank=True)
    osnovna_najemnina_ob_sklenitvi = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    def clean(self):
        super().clean()
        napake = {}

        if self.vat_regime_id and self.vat_regime.zahteva_referenco:
            if not (self.referenca_listine or "").strip():
                napake["referenca_listine"] = (
                    "Referenca listine je obvezna za ta DDV režim."
                )
            if self.datum_reference is None:
                napake["datum_reference"] = (
                    "Datum reference je obvezen za ta DDV režim."
                )

        if self.lease_id and self.lease.ima_klavzulo_indeksacije:
            if self.indeks_ob_osnovi is None:
                napake["indeks_ob_osnovi"] = (
                    "Izhodiščni indeks je obvezen pri indeksacijski klavzuli."
                )
            if self.datum_osnove_indeksacije is None:
                napake["datum_osnove_indeksacije"] = (
                    "Datum osnove indeksacije je obvezen pri indeksacijski klavzuli."
                )

        if napake:
            raise ValidationError(napake)


class RecipientContact(models.Model):
    class Vloga(models.TextChoices):
        TO = "TO", "TO"
        CC = "CC", "CC"
        BCC = "BCC", "BCC"

    class Kanal(models.TextChoices):
        PDF = "PDF", "PDF"
        ESLOG = "ESLOG", "eSLOG"
        OBOJE = "OBOJE", "Oboje"

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT)
    email = models.EmailField()
    vloga = models.CharField(max_length=3, choices=Vloga)
    kanal = models.CharField(max_length=5, choices=Kanal)
    aktiven = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if (
            self.lease_id
            and self.lease.tenant.tip == Tenant.Tip.FIZICNA_OSEBA
            and self.kanal in {self.Kanal.ESLOG, self.Kanal.OBOJE}
        ):
            raise ValidationError({"kanal": "Za fizično osebo je dovoljen samo PDF."})


class IndexationEvent(models.Model):
    lease_item = models.ForeignKey(LeaseItem, on_delete=models.PROTECT)
    datum_uskladitve = models.DateField()
    indeks_vrednost = models.DecimalField(max_digits=6, decimal_places=3)
    stara_vrednost = models.DecimalField(max_digits=10, decimal_places=2)
    nova_vrednost = models.DecimalField(max_digits=10, decimal_places=2)
    izkljucen = models.BooleanField(default=False)
    razlog_izkljucitve = models.TextField(blank=True)

    def clean(self):
        super().clean()
        if self.izkljucen and not (self.razlog_izkljucitve or "").strip():
            raise ValidationError(
                {"razlog_izkljucitve": "Razlog izključitve je obvezen."}
            )


class PoracunIndeksacije(models.Model):
    class Status(models.TextChoices):
        PREDLAGAN = "PREDLAGAN", "Predlagan"
        POTRJEN = "POTRJEN", "Potrjen"
        ZAVRNJEN = "ZAVRNJEN", "Zavrnjen"

    indexation_event = models.ForeignKey(IndexationEvent, on_delete=models.PROTECT)
    original_invoice = models.ForeignKey(
        "invoicing.Invoice", on_delete=models.PROTECT, related_name="poracuni_izvirnika"
    )
    prizadeto_obdobje_od = models.DateField()
    prizadeto_obdobje_do = models.DateField()
    znesek_poracuna = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=9, choices=Status, default=Status.PREDLAGAN)
    razlog_zavrnitve = models.TextField(blank=True)
    vkljucen_v_racun = models.ForeignKey(
        "invoicing.Invoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="vkljuceni_poracuni",
    )

    def clean(self):
        super().clean()
        if (
            self.status == self.Status.ZAVRNJEN
            and not (self.razlog_zavrnitve or "").strip()
        ):
            raise ValidationError({"razlog_zavrnitve": "Razlog zavrnitve je obvezen."})

        if self.indexation_event_id and self.znesek_poracuna is not None:
            pricakovano = (
                self.indexation_event.nova_vrednost
                - self.indexation_event.stara_vrednost
            )
            if self.znesek_poracuna != pricakovano:
                raise ValidationError(
                    {"znesek_poracuna": "Znesek poračuna se ne ujema z indeksacijo."}
                )

        if self.original_invoice_id and self.original_invoice.stevilka is None:
            raise ValidationError(
                {"original_invoice": "Originalni račun mora biti izdan."}
            )
