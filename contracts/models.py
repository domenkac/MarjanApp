from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction

from catalogs.models import VatRegime
from companies.models import Company
from core.eslog_konfiguracija import (
    EslogKonfiguracijaNapaka,
    normaliziraj_neobvezna_polja,
    normaliziraj_segmentne_nastavitve,
)
from core.models import FinancialQuerySet, validate_financial_save
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
    eslog_neobvezni_segmenti_override = models.JSONField(default=dict, blank=True)
    eslog_neobvezna_polja = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()
        napake = {}
        try:
            self.eslog_neobvezni_segmenti_override = (
                normaliziraj_segmentne_nastavitve(
                    self.eslog_neobvezni_segmenti_override
                )
            )
        except EslogKonfiguracijaNapaka as exc:
            napake["eslog_neobvezni_segmenti_override"] = str(exc)
        try:
            self.eslog_neobvezna_polja = normaliziraj_neobvezna_polja(
                self.eslog_neobvezna_polja
            )
        except EslogKonfiguracijaNapaka as exc:
            napake["eslog_neobvezna_polja"] = str(exc)
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
    objects = FinancialQuerySet.as_manager()

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

        if self.samodejno_podaljsanje and (
            self.obdobje_podaljsanja_mesecev is None
            or self.obdobje_podaljsanja_mesecev <= 0
        ):
            napake["obdobje_podaljsanja_mesecev"] = (
                "Obdobje podaljšanja mora biti pozitivno število mesecev."
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

        if self.pk and self.tenant_id:
            from .vat_eligibility import preveri_postavko_ddv

            previous_tenant = Lease.objects.filter(pk=self.pk).values_list(
                "tenant_id", flat=True
            ).first()
            if previous_tenant != self.tenant_id:
                for item in self.leaseitem_set.filter(arhivirana=False).select_related(
                    "vat_regime"
                ):
                    try:
                        preveri_postavko_ddv(item, tenant=self.tenant)
                    except ValidationError as exc:
                        napake.setdefault("tenant", []).extend(exc.messages)

        if napake:
            raise ValidationError(napake)

    def save(self, *args, **kwargs):
        edit = self.__dict__.get("_financial_edit")
        if edit is not None and self.pk is not None:
            from .renewals import save_lease_edit

            return save_lease_edit(self, source_fingerprint=edit[0], razlog=edit[1])
        self._nastavi_privzeti_rok_placila()
        from .renewals import validate_lease_edit

        with transaction.atomic():
            previous = type(self).objects.select_for_update().filter(pk=self.pk).first() if self.pk else None
            if previous:
                fields = kwargs.get("update_fields")
                changes = {
                    field.attname: getattr(self, field.attname)
                    for field in self._meta.fields
                    if fields is None or field.name in fields or field.attname in fields
                }
                validate_lease_edit(previous, changes, source_fingerprint=getattr(
                    self, "_edit_source_fingerprint", None,
                ))
            validate_financial_save(self, kwargs.get("update_fields"), kwargs.get("using"))
            result = super().save(*args, **kwargs)
            self.__dict__.pop("_edit_source_fingerprint", None)
            return result


class LeaseItem(models.Model):
    objects = FinancialQuerySet.as_manager()

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
    referencna_najemnina = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    datum_referencne_najemnine = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        from .services import pripravi_referencno_najemnino

        edit = self.__dict__.get("_financial_edit")
        if edit is not None and self.pk is not None:
            from .services import save_lease_item_edit

            return save_lease_item_edit(self, source_fingerprint=edit[0], razlog=edit[1])
        with transaction.atomic(using=kwargs.get("using") or self._state.db):
            validate_financial_save(self, kwargs.get("update_fields"), kwargs.get("using"))
            pripravi_referencno_najemnino(self)
            return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        napake = {}

        if (self.referencna_najemnina is None) != (self.datum_referencne_najemnine is None):
            napake["referencna_najemnina"] = "Referenčni znesek in datum morata biti določena skupaj."

        if self.vat_regime_id and self.lease_id:
            from .vat_eligibility import preveri_postavko_ddv

            try:
                preveri_postavko_ddv(self)
            except ValidationError as exc:
                napake["vat_regime"] = exc.messages

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

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["lease_item", "datum_uskladitve"],
                condition=models.Q(izkljucen=False),
                name="uniq_active_indexation_item_date",
            ),
        ]

    def clean(self):
        super().clean()
        if self.izkljucen and not (self.razlog_izkljucitve or "").strip():
            raise ValidationError(
                {"razlog_izkljucitve": "Razlog izključitve je obvezen."}
            )


class LeaseRenewalEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Zgodovina podaljšanj je nespremenljiva.")

    def delete(self):
        raise ValidationError("Zgodovina podaljšanj je nespremenljiva.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Zgodovina podaljšanj je nespremenljiva.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Podaljšanje zahteva izrecno domensko potrditev.")


class LeaseRenewalEvent(models.Model):
    class Decision(models.TextChoices):
        CONFIRMED = "CONFIRMED", "Potrjeno"

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="renewal_events")
    old_end = models.DateField()
    new_end = models.DateField()
    months = models.PositiveIntegerField()
    source_fingerprint = models.CharField(max_length=64)
    decision = models.CharField(max_length=9, choices=Decision, default=Decision.CONFIRMED)
    decided_at = models.DateTimeField()
    audit = models.OneToOneField("core.AuditLogEntry", on_delete=models.PROTECT)

    objects = LeaseRenewalEventQuerySet.as_manager()

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(fields=["lease", "old_end"], name="unique_lease_renewal_end"),
            models.UniqueConstraint(fields=["lease", "source_fingerprint"], name="unique_lease_renewal_source"),
            models.CheckConstraint(condition=models.Q(months__gt=0), name="renewal_positive_months"),
            models.CheckConstraint(condition=models.Q(new_end__gt=models.F("old_end")), name="renewal_end_increases"),
            models.CheckConstraint(condition=models.Q(decision="CONFIRMED"), name="renewal_confirmed_decision"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding or (self.pk and type(self).objects.filter(pk=self.pk).exists()):
            raise ValidationError("Zgodovina podaljšanj je nespremenljiva.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Zgodovina podaljšanj je nespremenljiva.")


class IndexationBatch(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "V pregledu"
        CONFIRMED = "CONFIRMED", "Potrjeno"

    company = models.ForeignKey("companies.Company", on_delete=models.PROTECT)
    index_year = models.PositiveSmallIntegerField()
    index_value = models.DecimalField(
        max_digits=6, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))],
    )
    effective_date = models.DateField()
    source_fingerprint = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=9, choices=State, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)


class IndexationDecision(models.Model):
    class Outcome(models.TextChoices):
        PENDING = "PENDING", "V pregledu"
        APPROVED = "APPROVED", "Potrjeno"
        EXCLUDED = "EXCLUDED", "Pogodba izključena"
        SKIPPED = "SKIPPED", "Preskočeno"

    batch = models.ForeignKey(IndexationBatch, on_delete=models.PROTECT, related_name="decisions")
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT)
    lease_item = models.ForeignKey(LeaseItem, on_delete=models.PROTECT, null=True, blank=True)
    proposal = models.JSONField()
    selected = models.BooleanField(default=False)
    exclusion_reason = models.TextField(blank=True)
    outcome = models.CharField(max_length=8, choices=Outcome, default=Outcome.PENDING)
    reason = models.TextField(blank=True)
    event = models.OneToOneField(IndexationEvent, on_delete=models.PROTECT, null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)


_ADJUSTMENT_EVIDENCE_FIELDS = frozenset({
    "indexation_event", "indexation_event_id", "original_invoice", "original_invoice_id",
    "source_line", "source_line_id", "prizadeto_obdobje_od", "prizadeto_obdobje_do",
    "znesek_poracuna", "calculation_evidence", "detection_month", "detected_at",
})
_ADJUSTMENT_OBSERVATION_FIELDS = frozenset({
    "evidence_state", "unresolved_reasons", "last_observation_fingerprint",
})
_ADJUSTMENT_DECISION_FIELDS = frozenset({"status", "razlog_zavrnitve"})


class AdjustmentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if _ADJUSTMENT_DECISION_FIELDS.intersection(kwargs):
            raise ValidationError("Odločitve poračuna zahtevajo revidirano domensko storitev.")
        if (_ADJUSTMENT_EVIDENCE_FIELDS | _ADJUSTMENT_OBSERVATION_FIELDS).intersection(kwargs):
            raise ValidationError("Dokazila poračuna so nespremenljiva.")
        if {"vkljucen_v_racun", "vkljucen_v_racun_id"}.intersection(kwargs):
            raise ValidationError("Samodejno vključevanje poračunov še ni omogočeno.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if _ADJUSTMENT_DECISION_FIELDS.intersection(fields):
            raise ValidationError("Odločitve poračuna zahtevajo revidirano domensko storitev.")
        if (_ADJUSTMENT_EVIDENCE_FIELDS | _ADJUSTMENT_OBSERVATION_FIELDS).intersection(fields):
            raise ValidationError("Dokazila poračuna so nespremenljiva.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        from .adjustments import validate_bulk_adjustments

        objs = list(objs)
        validate_bulk_adjustments(objs, update_conflicts=update_conflicts)
        return super().bulk_create(
            objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts, update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        raise ValidationError("Izbris zgodovine poračunov ni dovoljen.")


class PoracunIndeksacije(models.Model):
    class Status(models.TextChoices):
        PREDLAGAN = "PREDLAGAN", "Predlagan"
        POTRJEN = "POTRJEN", "Potrjen"
        ZAVRNJEN = "ZAVRNJEN", "Zavrnjen"

    class EvidenceState(models.TextChoices):
        LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED", "Nepreverjena zgodovinska dokazila"
        VERIFIED = "VERIFIED", "Preverjena dokazila"
        UNRESOLVED = "UNRESOLVED", "Potrebna uskladitev dokazil"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Obvezen ročni pregled"

    objects = AdjustmentQuerySet.as_manager()

    indexation_event = models.ForeignKey(IndexationEvent, on_delete=models.PROTECT)
    original_invoice = models.ForeignKey(
        "invoicing.Invoice", on_delete=models.PROTECT, related_name="poracuni_izvirnika"
    )
    prizadeto_obdobje_od = models.DateField()
    prizadeto_obdobje_do = models.DateField()
    znesek_poracuna = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=9, choices=Status, default=Status.PREDLAGAN)
    razlog_zavrnitve = models.TextField(blank=True)
    vkljucen_v_racun = models.ForeignKey(
        "invoicing.Invoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="vkljuceni_poracuni",
    )
    source_line = models.ForeignKey(
        "invoicing.InvoiceLine", on_delete=models.PROTECT, null=True, blank=True,
        related_name="indexation_adjustments", editable=False,
    )
    calculation_evidence = models.JSONField(default=dict, blank=True, editable=False)
    detection_month = models.DateField(null=True, blank=True, editable=False)
    detected_at = models.DateTimeField(null=True, blank=True, editable=False)
    evidence_state = models.CharField(
        max_length=17, choices=EvidenceState, default=EvidenceState.LEGACY_UNVERIFIED,
    )
    unresolved_reasons = models.JSONField(default=list, blank=True)
    last_observation_fingerprint = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["indexation_event", "source_line"],
                name="uniq_adjustment_event_source_line",
            ),
        ]

    def clean(self):
        super().clean()
        from .adjustments import validate_adjustment

        validate_adjustment(self)

    def save(self, *args, **kwargs):
        from .adjustments import save_adjustment

        return save_adjustment(self, *args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Izbris zgodovine poračunov ni dovoljen.")
